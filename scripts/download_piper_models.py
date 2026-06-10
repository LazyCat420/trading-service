#!/usr/bin/env python3
import os
import urllib.request
import json
import argparse

VOICES_JSON_URL = "https://huggingface.co/rhasspy/piper-voices/raw/v1.0.0/voices.json"
BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/"

def download_file(url, dest):
    if os.path.exists(dest):
        print(f"Already exists: {os.path.basename(dest)}")
        return True
    try:
        print(f"Downloading {url} to {os.path.basename(dest)}...")
        urllib.request.urlretrieve(url, dest)
        return True
    except Exception as e:
        print(f"Failed to download {url}: {e}")
        if os.path.exists(dest):
            os.remove(dest)
        return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=str, default=os.path.join(os.path.dirname(__file__), "..", "data", "piper_models"))
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    print("Fetching voices.json...")
    req = urllib.request.urlopen(VOICES_JSON_URL)
    data = json.loads(req.read().decode("utf-8"))

    en_voices = {k: v for k, v in data.items() if k.startswith("en_")}
    print(f"Found {len(en_voices)} English voices.")

    for name, info in en_voices.items():
        # Get relative files
        files = info.get("files", {})
        onnx_file = None
        json_file = None
        
        for f in files.keys():
            if f.endswith(".onnx"):
                onnx_file = f
            elif f.endswith(".onnx.json"):
                json_file = f

        if not onnx_file or not json_file:
            print(f"Skipping {name}, missing files in voices.json")
            continue

        onnx_url = BASE_URL + onnx_file
        json_url = BASE_URL + json_file

        onnx_dest = os.path.join(args.output_dir, f"{name}.onnx")
        json_dest = os.path.join(args.output_dir, f"{name}.onnx.json")

        download_file(onnx_url, onnx_dest)
        download_file(json_url, json_dest)

    print("All English Piper models downloaded successfully.")

if __name__ == "__main__":
    main()
