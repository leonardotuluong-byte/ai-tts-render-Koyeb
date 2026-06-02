import gradio as gr
import asyncio
import edge_tts
import srt
import os
import tempfile
import io
import subprocess
import threading
from pydub import AudioSegment, silence
from pydub.effects import normalize
from pydub.scipy_effects import low_pass_filter

# =========================================================================
# 1. LẤY DANH SÁCH GIỌNG ĐỌC (Đã xử lý chống sập luồng trên Server)
# =========================================================================
def get_voices_sync():
    result = []
    def fetch_voices():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            voices = loop.run_until_complete(edge_tts.list_voices())
            result.extend(voices)
            loop.close()
        except Exception as e:
            print(f"Lỗi lấy giọng đọc: {e}")

    t = threading.Thread(target=fetch_voices)
    t.start()
    t.join()
    return result

ALL_VOICES = get_voices_sync()
LOCALES = sorted(list(set([v['Locale'] for v in ALL_VOICES]))) if ALL_VOICES else ["es-ES"]
DEFAULT_LOCALE = "es-ES" if "es-ES" in LOCALES else LOCALES[0]
initial_voices = [v['ShortName'] for v in ALL_VOICES] if ALL_VOICES else []

def update_voice_list(lang, gender):
    filtered = [v for v in ALL_VOICES if v['Locale'] == lang]
    if gender != "All":
        filtered = [v for v in filtered if v['Gender'] == gender]
    
    choices = [v['ShortName'] for v in filtered]
    default_val = choices[0] if choices else None
    return gr.update(choices=choices, value=default_val)

def trim_audio_silence(audio_segment):
    if len(audio_segment) < 100: 
        return audio_segment
    try:
        start_trim = silence.detect_leading_silence(audio_segment, silence_threshold=-40.0)
        end_trim = silence.detect_leading_silence(audio_segment.reverse(), silence_threshold=-40.0)
        
        start_trim = max(0, start_trim - 20) 
        end_trim = max(0, end_trim - 20)

        if start_trim + end_trim >= len(audio_segment):
            return audio_segment
            
        return audio_segment[start_trim:len(audio_segment)-end_trim]
    except Exception as e:
        return audio_segment

# =========================================================================
# 2. HÀM CO GIÃN THỜI GIAN CHUYÊN NGHIỆP BẰNG FFMPEG (ATEMPO)
# =========================================================================
def stretch_audio_ffmpeg(audio_segment, factor):
    if factor <= 1.0:
        return audio_segment
    
    safe_factor = min(factor, 1.35)
    
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_in, \
         tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_out:
        try:
            audio_segment.export(temp_in.name, format="wav")
            cmd = [
                "ffmpeg", "-y", 
                "-i", temp_in.name, 
                "-filter:a", f"atempo={safe_factor}", 
                temp_out.name
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            stretched_seg = AudioSegment.from_wav(temp_out.name)
            return stretched_seg
        except Exception:
            return audio_segment
        finally:
            if os.path.exists(temp_in.name): os.remove(temp_in.name)
            if os.path.exists(temp_out.name): os.remove(temp_out.name)

# =========================================================================
# 3. HÀM GỌI TTS BẤT ĐỒNG BỘ NGUYÊN BẢN
# =========================================================================
async def run_tts_with_retry(text, voice_code, retries=5):
    text = text.strip()
    if not text: 
        return None
        
    for attempt in range(retries):
        try:
            communicate = edge_tts.Communicate(text, voice_code, rate="+5%")
            audio_data = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_data.extend(chunk["data"])
                    
            if audio_data: 
                return bytes(audio_data)
        except Exception:
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
            
    return None

# =========================================================================
# 4. LÕI XỬ LÝ ÂM THANH CHÍNH
# =========================================================================
async def process_audio(input_text, voice):
    if not input_text or not input_text.strip():
        return None, "❌ Vui lòng nhập kịch bản!"
    if not voice:
        return None, "❌ Vui lòng chọn giọng đọc!"
    
    try:
        is_srt = " --> " in input_text
        
        if not is_srt:
            res = await run_tts_with_retry(input_text, voice)
            if res:
                seg = AudioSegment.from_file(io.BytesIO(res), format="mp3")
                seg = seg.set_frame_rate(44100).set_channels(1).set_sample_width(2)
                seg = low_pass_filter(seg, cutoff_freq=8000)
                seg = normalize(seg)
                
                temp_out = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
                seg.export(temp_out.name, format="mp3", bitrate="192k")
                return temp_out.name, "✅ Xử lý văn bản thường thành công!"
            return None, "❌ Lỗi: Không thể tải giọng đọc từ máy chủ."
            
        else:
            subs = list(srt.parse(input_text))
            if not subs:
                return None, "❌ SRT không hợp lệ!"
            
            final_audio = AudioSegment.silent(duration=0, frame_rate=44100).set_channels(1).set_sample_width(2)
            failed_lines = []
            
            for i, sub in enumerate(subs):
                text_content = sub.content.strip()
                if not text_content: 
                    continue
                
                chunk = await run_tts_with_retry(text_content, voice)
                
                if chunk:
                    seg = AudioSegment.from_file(io.BytesIO(chunk), format="mp3")
                    seg = seg.set_frame_rate(44100).set_channels(1).set_sample_width(2)
                    
                    seg = normalize(seg) 
                    seg = trim_audio_silence(seg)
                    
                    srt_start_ms = int(sub.start.total_seconds() * 1000)
                    duration_allowed_ms = (sub.end - sub.start).total_seconds() * 1000
                    
                    if duration_allowed_ms <= 0: 
                        duration_allowed_ms = 500

                    actual_duration_ms = len(seg)
                    
                    if actual_duration_ms > duration_allowed_ms:
                        factor = actual_duration_ms / duration_allowed_ms
                        seg = stretch_audio_ffmpeg(seg, factor)
                    
                    if len(final_audio) < srt_start_ms:
                        gap = srt_start_ms - len(final_audio)
                        final_audio += AudioSegment.silent(duration=gap, frame_rate=44100)
                    
                    final_audio += seg
                else:
                    failed_lines.append(f"Dòng {i+1}")
                
                await asyncio.sleep(0.5)

            final_audio = low_pass_filter(final_audio, cutoff_freq=8000)
            final_audio = normalize(final_audio)
            
            temp_out = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            final_audio.export(temp_out.name, format="mp3", bitrate="192k")
            
            if failed_lines:
                return temp_out.name, f"⚠️ Xong, nhưng mất {len(failed_lines)} câu."
            return temp_out.name, "✅ Hoàn thành xuất sắc!"
            
    except Exception as e:
        return None, f"❌ Lỗi hệ thống: {str(e)}"

# =========================================================================
# GIAO DIỆN
# =========================================================================
with gr.Blocks(theme=gr.themes.Base()) as app:
    gr.Markdown("<h2 style='text-align: center; color: #22c55e;'>🎙️ AI TTS Pro - Nền Tảng Mới Ổn Định</h2>")
    
    with gr.Row():
        with gr.Column(scale=2):
            gr.Markdown("### 📝 Kịch bản (Hỗ trợ Văn bản & SRT)")
            input_text = gr.Textbox(lines=14, label="Dán nội dung vào đây", placeholder="Nhập văn bản hoặc dán file SRT...")
            process_btn = gr.Button("🚀 BẮT ĐẦU LỒNG TIẾNG", variant="primary", size="lg")
            
        with gr.Column(scale=1):
            gr.Markdown("### ⚙️ Cấu hình Giọng đọc")
            with gr.Row():
                lang_filter = gr.Dropdown(choices=LOCALES, value=DEFAULT_LOCALE, label="🌐 Ngôn ngữ")
                gender_filter = gr.Radio(choices=["All", "Male", "Female"], value="All", label="👤 Giới tính")
            
            voice_dropdown = gr.Dropdown(choices=initial_voices, value=initial_voices[0] if initial_voices else None, label="🎙️ Chọn giọng đọc", interactive=True)
            
            gr.Markdown("---")
            gr.Markdown("### 🎵 Kết quả")
            output_audio = gr.Audio(label="File hoàn chỉnh (.mp3)", type="filepath")
            status_text = gr.Textbox(label="Trạng thái thực thi", interactive=False)

    lang_filter.change(fn=update_voice_list, inputs=[lang_filter, gender_filter], outputs=[voice_dropdown])
    gender_filter.change(fn=update_voice_list, inputs=[lang_filter, gender_filter], outputs=[voice_dropdown])
    
    process_btn.click(
        fn=process_audio,
        inputs=[input_text, voice_dropdown],
        outputs=[output_audio, status_text]
    )

if __name__ == "__main__":
    # Cấu hình mở cổng 8000 để chạy mượt trên Cloud
    app.queue().launch(server_name="0.0.0.0", server_port=8000)
