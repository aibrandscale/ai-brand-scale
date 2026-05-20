#!/usr/bin/env python3
"""
Pixar Video Ad Pipeline (Kie AI Edition)
═════════════════════════════════════════
Product info → Gemini Script → Nano Banana Pro Images → ElevenLabs Voice → Kling Video → Google Drive

All image/video generation goes through Kie AI (api.kie.ai) — single API key.
Voice via ElevenLabs.

Usage:
  Interactive:  python pixar_video_pipeline.py
  CLI:          python pixar_video_pipeline.py --product "Hydrogen Water Bottle" --description "..." --scenes 6
"""

import os, sys, json, time, re, base64, requests, argparse
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / '.env')

# ─── CONFIG ────────────────────────────────────────────────
KIE_API_KEY       = os.getenv('KIE_AI_API_KEY', '')
DRIVE_FOLDER_ID   = os.getenv('GOOGLE_DRIVE_FOLDER_ID', '')

KIE_BASE          = "https://api.kie.ai"
KIE_TASK_URL      = f"{KIE_BASE}/api/v1/jobs/createTask"
KIE_STATUS_URL    = f"{KIE_BASE}/api/v1/jobs/recordInfo"
KIE_CHAT_URL      = f"{KIE_BASE}/gemini-2.5-pro/v1/chat/completions"

TMP_DIR = Path(__file__).parent.parent / '.tmp' / 'pixar_pipeline'
TMP_DIR.mkdir(parents=True, exist_ok=True)


def kie_headers():
    return {
        "Authorization": f"Bearer {KIE_API_KEY}",
        "Content-Type": "application/json",
    }


# ═══════════════════════════════════════════════════════════
# KIE AI — UNIFIED TASK HELPER (poll until done)
# ═══════════════════════════════════════════════════════════

def kie_create_task(model: str, input_data: dict) -> str:
    """Create an async task on Kie AI. Returns taskId."""
    payload = {
        "model": model,
        "input": input_data,
    }
    resp = requests.post(KIE_TASK_URL, headers=kie_headers(), json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        raise Exception(f"Kie AI error: {data.get('msg', 'Unknown error')}")
    return data["data"]["taskId"]


def kie_poll_task(task_id: str, max_wait: int = 600, interval: int = 5) -> list:
    """Poll a Kie AI task until success. Returns list of result URLs."""
    start = time.time()

    while time.time() - start < max_wait:
        resp = requests.get(
            f"{KIE_STATUS_URL}?taskId={task_id}",
            headers={"Authorization": f"Bearer {KIE_API_KEY}"},
            timeout=30,
        )

        if resp.status_code == 200:
            data = resp.json().get("data", {})
            state = data.get("state", "")

            if state == "success":
                result_json = data.get("resultJson", "{}")
                if isinstance(result_json, str):
                    result_json = json.loads(result_json)
                return result_json.get("resultUrls", [])

            elif state in ("fail", "failed"):
                fail_msg = data.get("failMsg", "Unknown error")
                print(f"   ❌ Task failed: {fail_msg}")
                return []

            progress = data.get("progress", 0)
            elapsed = int(time.time() - start)
            print(f"   ⏳ {state} — {progress}% ({elapsed}s)   ", end='\r')

        time.sleep(interval)

    print(f"\n   ❌ Timeout after {max_wait}s")
    return []


# ═══════════════════════════════════════════════════════════
# 1. SCRIPT & SCENE GENERATOR (Gemini 2.5 Pro via Kie)
# ═══════════════════════════════════════════════════════════

def generate_script(product_name: str, product_description: str, num_scenes: int = 7,
                    tone: str = "fun, emotional, Pixar-style", language: str = "bg") -> dict:
    """Generate a script with scenes using Gemini 2.5 Pro through Kie AI."""

    lang_note = "Скриптът и диалозите ТРЯБВА да са на БЪЛГАРСКИ." if language == "bg" else f"Script in {language}."

    prompt = f"""You are a Pixar-level creative director creating a short animated ad.

PRODUCT: {product_name}
DESCRIPTION: {product_description}
TONE: {tone}

{lang_note}

Create exactly {num_scenes} scenes for a short animated ad (30-60 sec total).
The product is the MAIN CHARACTER — it has eyes, a mouth, and personality.
Think Pixar style: expressive, cute, emotional storytelling.

For each scene return:
1. "scene_number": 1-{num_scenes}
2. "visual_description": Detailed Pixar-style visual description in ENGLISH (for image generation).
   Include: character pose, expression, background, lighting, camera angle.
   ALWAYS describe the product-character consistently: same eyes style, mouth, color scheme.
3. "dialogue": What the character says in this scene (in {'Bulgarian' if language == 'bg' else language}).
   Keep it short — 1-2 sentences per scene max.
4. "emotion": The emotion/mood of this scene (e.g. "excited", "sad", "triumphant")
5. "duration_seconds": Estimated duration (4-8 seconds per scene)

Also return:
- "character_prompt": A detailed, reusable Pixar character description of the product-as-character.
  This will be prepended to every scene's image prompt for consistency.
  Include: exact colors, eye style, mouth style, body proportions, texture.
- "title": A catchy title for the ad

Return valid JSON only. Structure:
{{
  "title": "...",
  "character_prompt": "...",
  "total_duration_seconds": N,
  "scenes": [
    {{
      "scene_number": 1,
      "visual_description": "...",
      "dialogue": "...",
      "emotion": "...",
      "duration_seconds": N
    }}
  ]
}}"""

    print("\n🎬 Generating script with Gemini 2.5 Pro...")

    resp = requests.post(
        KIE_CHAT_URL,
        headers=kie_headers(),
        json={
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "reasoning_effort": "high",
        },
        timeout=90,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]

    # Extract JSON from response (handle markdown code blocks)
    json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
    if json_match:
        content = json_match.group(1)
    else:
        # Try to find raw JSON
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if json_match:
            content = json_match.group(0)

    script = json.loads(content)

    # Save script
    script_path = TMP_DIR / f"{sanitize(product_name)}_script.json"
    script_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"   ✅ Script saved: {script_path}")

    return script


def print_script_for_approval(script: dict):
    """Pretty-print script for user review."""
    print("\n" + "═" * 60)
    print(f"  🎬 {script['title']}")
    print(f"  ⏱  Total: ~{script['total_duration_seconds']}s")
    print("═" * 60)
    print(f"\n🧸 Character: {script['character_prompt'][:120]}...")

    for s in script["scenes"]:
        print(f"\n{'─' * 50}")
        print(f"  📌 Scene {s['scene_number']}  |  {s['emotion']}  |  ~{s['duration_seconds']}s")
        print(f"  🎨 {s['visual_description'][:100]}...")
        print(f"  💬 \"{s['dialogue']}\"")

    print("\n" + "═" * 60)


# ═══════════════════════════════════════════════════════════
# 2. NANO BANANA PRO IMAGE GENERATOR (character-consistent)
# ═══════════════════════════════════════════════════════════

def generate_scene_images(script: dict, product_name: str) -> list:
    """Generate Pixar-style images for each scene using Nano Banana Pro via Kie AI."""

    character = script["character_prompt"]
    images = []

    for scene in script["scenes"]:
        n = scene["scene_number"]
        print(f"\n🎨 Generating image for Scene {n}/{len(script['scenes'])}...")

        full_prompt = f"""Pixar-style 3D animated scene. HIGH QUALITY, cinematic lighting,
rendered in Pixar/Disney animation style.

CHARACTER (MUST be consistent across all scenes):
{character}

THIS SCENE:
{scene['visual_description']}

MOOD: {scene['emotion']}

IMPORTANT:
- The character is a {product_name} with cute eyes and an expressive mouth.
- Pixar/Disney 3D animation quality.
- 16:9 aspect ratio composition.
- No text or watermarks."""

        # Create async task
        task_id = kie_create_task("nano-banana-pro", {
            "prompt": full_prompt,
            "image_input": [],
            "aspect_ratio": "16:9",
            "resolution": "1K",
            "output_format": "png",
        })
        print(f"   📋 Task: {task_id}")

        # Poll for result
        result_urls = kie_poll_task(task_id, max_wait=120, interval=4)

        if result_urls:
            img_url = result_urls[0]
            # Download image
            img_data = requests.get(img_url, timeout=60).content
            img_path = TMP_DIR / f"scene_{n:02d}.png"
            img_path.write_bytes(img_data)
            images.append({"scene": n, "path": str(img_path), "url": img_url})
            print(f"   ✅ Scene {n} saved: {img_path.name}")
        else:
            print(f"   ❌ Failed to generate scene {n}")
            images.append({"scene": n, "path": None, "url": None})

        time.sleep(1)  # Rate limiting

    return images


# ═══════════════════════════════════════════════════════════
# 3. ELEVENLABS VOICEOVER (via Kie AI)
# ═══════════════════════════════════════════════════════════

# Popular voices for Bulgarian/multilingual content
VOICE_PRESETS = {
    "rachel": "Rachel",
    "aria": "Aria",
    "roger": "Roger",
    "george": "George",
    "sarah": "Sarah",
    "charlie": "Charlie",
    "matilda": "Matilda",
}


def generate_voiceover(script: dict, voice_name: str = None) -> list:
    """Generate voiceover audio for each scene using ElevenLabs via Kie AI."""

    voice = voice_name or "Rachel"
    audio_files = []

    for scene in script["scenes"]:
        n = scene["scene_number"]
        dialogue = scene["dialogue"]

        if not dialogue.strip():
            print(f"   ⏭ Scene {n}: no dialogue, skipping")
            audio_files.append({"scene": n, "path": None})
            continue

        print(f"\n🎙 Generating voice for Scene {n}: \"{dialogue[:50]}\"...")

        # Create TTS task through Kie AI
        task_id = kie_create_task("elevenlabs/text-to-speech-multilingual-v2", {
            "text": dialogue,
            "voice": voice,
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.4,
        })

        # Poll (TTS is fast — ~10-15s)
        result_urls = kie_poll_task(task_id, max_wait=60, interval=3)

        if result_urls:
            audio_data = requests.get(result_urls[0], timeout=30).content
            audio_path = TMP_DIR / f"voice_{n:02d}.mp3"
            audio_path.write_bytes(audio_data)
            audio_files.append({"scene": n, "path": str(audio_path)})
            print(f"   ✅ Voice saved: {audio_path.name} ({len(audio_data)//1024} KB)")
        else:
            print(f"   ❌ Voice generation failed for scene {n}")
            audio_files.append({"scene": n, "path": None})

        time.sleep(0.5)

    return audio_files


# ═══════════════════════════════════════════════════════════
# 4. KLING 2.6 IMAGE-TO-VIDEO (via Kie AI)
# ═══════════════════════════════════════════════════════════

def generate_video_for_scene(image_url: str, scene: dict, scene_num: int) -> str:
    """Generate video from image using Kling 2.6 image-to-video via Kie AI."""

    print(f"\n🎬 Generating video for Scene {scene_num}...")

    prompt = (
        f"The character is speaking and expressing emotion: {scene['emotion']}. "
        f"Subtle mouth movement, expressive eyes, Pixar animation style. "
        f"Gentle body language and smooth motion."
    )

    task_id = kie_create_task("kling-2.6/image-to-video", {
        "prompt": prompt,
        "image_urls": [image_url],
        "sound": False,
        "duration": "5",
    })
    print(f"   📋 Task: {task_id}")

    # Video takes longer — poll with longer interval
    result_urls = kie_poll_task(task_id, max_wait=600, interval=10)

    if result_urls:
        video_url = result_urls[0]
        video_data = requests.get(video_url, timeout=120).content
        video_path = TMP_DIR / f"video_{scene_num:02d}.mp4"
        video_path.write_bytes(video_data)
        print(f"   ✅ Video saved: {video_path.name}")
        return str(video_path)

    print(f"   ❌ Video generation failed for scene {scene_num}")
    return None


# ═══════════════════════════════════════════════════════════
# 5. GOOGLE DRIVE UPLOADER
# ═══════════════════════════════════════════════════════════

def upload_to_drive(files: list, project_name: str, folder_id: str = None):
    """Upload finished videos to Google Drive with proper naming."""

    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from meta_ads_launcher import get_drive_service
        drive = get_drive_service()
    except Exception as e:
        print(f"\n⚠️ Google Drive not configured: {e}")
        print("   Videos are saved locally in .tmp/pixar_pipeline/")
        return None

    target_folder = folder_id or DRIVE_FOLDER_ID

    # Create project subfolder
    folder_meta = {
        'name': f"Pixar Ad — {project_name}",
        'mimeType': 'application/vnd.google-apps.folder',
    }
    if target_folder:
        folder_meta['parents'] = [target_folder]

    folder = drive.files().create(body=folder_meta, fields='id').execute()
    created_folder_id = folder['id']
    print(f"\n📁 Created Drive folder: 'Pixar Ad — {project_name}'")

    from googleapiclient.http import MediaFileUpload

    for i, f in enumerate(files):
        if not f or not Path(f).exists():
            continue

        file_name = f"кадър {i+1}{Path(f).suffix}"
        media = MediaFileUpload(f, resumable=True)
        file_meta = {
            'name': file_name,
            'parents': [created_folder_id],
        }

        drive.files().create(body=file_meta, media_body=media, fields='id,name').execute()
        print(f"   ✅ Uploaded: {file_name}")

    drive_url = f"https://drive.google.com/drive/folders/{created_folder_id}"
    print(f"\n🔗 Drive folder: {drive_url}")
    return drive_url


# ═══════════════════════════════════════════════════════════
# 6. MAIN PIPELINE
# ═══════════════════════════════════════════════════════════

def sanitize(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9а-яА-Я]', '_', name)[:40]


def check_api_keys():
    """Check which API keys are configured."""
    keys = {
        'Kie AI (images + script + video + voice)': bool(KIE_API_KEY),
        'Google Drive (upload)':                     bool(DRIVE_FOLDER_ID),
    }

    print("\n🔑 API Key Status:")
    all_ok = True
    for name, ok in keys.items():
        status = "✅" if ok else "❌ MISSING"
        print(f"   {status}  {name}")
        if not ok and name != 'Google Drive (upload)':
            all_ok = False

    if not all_ok:
        print("\n⚠️  Add missing key to .env file:")
        print("   KIE_AI_API_KEY=...")

    return all_ok


def run_pipeline(product_name: str, product_description: str,
                 num_scenes: int = 7, voice_name: str = None,
                 skip_approval: bool = False, language: str = "bg"):
    """Run the full Pixar video pipeline."""

    print("\n" + "═" * 60)
    print("  🎬 PIXAR VIDEO AD PIPELINE (Kie AI Edition)")
    print("═" * 60)
    print(f"  Product:  {product_name}")
    print(f"  Scenes:   {num_scenes}")
    print(f"  Language:  {language}")
    print(f"  Images:    Nano Banana Pro")
    print(f"  Video:     Kling 2.6")
    print(f"  Voice:     ElevenLabs")
    print("═" * 60)

    if not check_api_keys():
        print("\n❌ Fix missing API keys before running the pipeline.")
        return

    # ── STEP 1: Generate Script ──
    print("\n\n📝 STEP 1/5: GENERATING SCRIPT (Gemini 2.5 Pro)...")
    script = generate_script(product_name, product_description, num_scenes, language=language)
    print_script_for_approval(script)

    # ── APPROVAL GATE ──
    if not skip_approval:
        print("\n🤔 Approve this script?")
        choice = input("   [Y] Yes, continue  |  [R] Regenerate  |  [E] Edit  |  [Q] Quit: ").strip().upper()

        if choice == 'R':
            print("\n🔄 Regenerating...")
            script = generate_script(product_name, product_description, num_scenes, language=language)
            print_script_for_approval(script)
        elif choice == 'E':
            script_path = TMP_DIR / f"{sanitize(product_name)}_script.json"
            print(f"\n📝 Edit the script file and save:")
            print(f"   {script_path}")
            input("   Press Enter when done editing...")
            script = json.loads(script_path.read_text(encoding='utf-8'))
        elif choice == 'Q':
            print("👋 Cancelled.")
            return

    # ── STEP 2: Generate Images ──
    print("\n\n🎨 STEP 2/5: GENERATING PIXAR IMAGES (Nano Banana Pro)...")
    images = generate_scene_images(script, product_name)

    valid_images = [img for img in images if img.get("path")]
    if not valid_images:
        print("❌ No images generated. Aborting.")
        return

    print(f"\n   ✅ {len(valid_images)} images generated. Check them in: {TMP_DIR}")

    if not skip_approval:
        choice = input("\n   Images OK? [Y] Continue  |  [Q] Quit: ").strip().upper()
        if choice == 'Q':
            return

    # ── STEP 3: Generate Voiceover ──
    print("\n\n🎙 STEP 3/5: GENERATING VOICEOVER (ElevenLabs via Kie AI)...")
    audio_files = generate_voiceover(script, voice_name=voice_name)

    if not skip_approval:
        choice = input("\n   Voice OK? [Y] Continue  |  [Q] Quit: ").strip().upper()
        if choice == 'Q':
            return

    # ── STEP 4: Generate Videos ──
    print("\n\n🎬 STEP 4/5: GENERATING VIDEOS (Kling 2.6)...")
    print("   ⏳ This takes 2-5 minutes per scene...")
    video_files = []

    for img in images:
        if not img.get("url"):
            print(f"   ⏭ Scene {img['scene']}: no image, skipping")
            video_files.append(None)
            continue

        scene_data = script["scenes"][img["scene"] - 1]
        video_path = generate_video_for_scene(
            img["url"], scene_data, img["scene"]
        )
        video_files.append(video_path)

    # ── STEP 5: Upload to Drive ──
    print("\n\n📁 STEP 5/5: UPLOADING TO GOOGLE DRIVE...")
    valid_videos = [v for v in video_files if v]

    if valid_videos:
        drive_url = upload_to_drive(valid_videos, product_name)
    else:
        print("   ⚠️ No videos to upload. Check .tmp/pixar_pipeline/ for intermediate files.")
        drive_url = None

    # ── SUMMARY ──
    print("\n\n" + "═" * 60)
    print("  ✅ PIPELINE COMPLETE!")
    print("═" * 60)
    print(f"  📝 Script:  {len(script['scenes'])} scenes")
    print(f"  🎨 Images:  {len(valid_images)} generated (Nano Banana Pro)")
    print(f"  🎙 Voice:   {len([a for a in audio_files if a.get('path')])} generated (ElevenLabs)")
    print(f"  🎬 Videos:  {len(valid_videos)} generated (Kling 2.6)")
    print(f"  📁 Local:   {TMP_DIR}")
    if drive_url:
        print(f"  🔗 Drive:   {drive_url}")
    print("═" * 60)


# ═══════════════════════════════════════════════════════════
# CLI / INTERACTIVE
# ═══════════════════════════════════════════════════════════

def interactive_mode():
    """Interactive mode — ask user for product details."""
    print("\n" + "═" * 60)
    print("  🎬 PIXAR VIDEO AD PIPELINE — Interactive Mode")
    print("  📦 Powered by: Kie AI + ElevenLabs")
    print("═" * 60)

    check_api_keys()

    product_name = input("\n📦 Product name: ").strip()
    if not product_name:
        print("❌ Product name required.")
        return

    print("📄 Product description (paste, then press Enter twice):")
    lines = []
    while True:
        line = input()
        if not line and lines:
            break
        lines.append(line)
    product_description = "\n".join(lines)

    num_scenes = input("\n🎬 Number of scenes [7]: ").strip()
    num_scenes = int(num_scenes) if num_scenes.isdigit() else 7
    num_scenes = min(max(num_scenes, 3), 10)

    language = input("🌍 Language [bg]: ").strip() or "bg"

    # Voice selection
    print("\n🎙 Available voices: Rachel, Aria, Roger, George, Sarah, Charlie, Matilda")
    voice_name = input("   Voice name [Rachel]: ").strip() or "Rachel"

    run_pipeline(product_name, product_description, num_scenes, voice_name, language=language)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pixar Video Ad Pipeline (Kie AI)")
    parser.add_argument("--product", help="Product name")
    parser.add_argument("--description", help="Product description")
    parser.add_argument("--scenes", type=int, default=7, help="Number of scenes (3-10)")
    parser.add_argument("--voice", default="Rachel", help="Voice name (Rachel, Aria, Roger, etc.)")
    parser.add_argument("--language", default="bg", help="Script language (default: bg)")
    parser.add_argument("--auto", action="store_true", help="Skip approval gates")
    args = parser.parse_args()

    if args.product and args.description:
        run_pipeline(args.product, args.description, args.scenes,
                     args.voice, skip_approval=args.auto, language=args.language)
    else:
        interactive_mode()
