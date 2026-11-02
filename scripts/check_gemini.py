"""Check that Gemini works with the settings in .env, without printing the key.

Run from the repo folder:  python scripts/check_gemini.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from campuspulse.config import settings  # noqa: E402  (loads .env)


def main() -> int:
    key = settings.gemini_api_key or ""
    print(f".env found:        {os.path.isfile('.env')}")
    print(f"Using Vertex AI:   {settings.gemini_vertex}")
    print(f"Model:             {settings.gemini_model}")
    if not settings.gemini_vertex:
        looks_ok = key.startswith("AIza") and len(key) == 39 and key.strip() == key
        print(f"API key set:       {bool(key)} (length {len(key)}, "
              f"{'looks right' if looks_ok else 'expected 39 characters starting with AIza'})")
    if not settings.gemini_enabled:
        print("\nGemini is not configured. Put GEMINI_API_KEY=... in .env (same folder as README.md).")
        return 1

    from google import genai
    from google.genai import types

    client = (genai.Client(vertexai=True, project=settings.gcp_project, location=settings.gemini_location)
              if settings.gemini_vertex else genai.Client(api_key=key))

    tests = [
        ("plain request", None),
        ("JSON mode (used by the brief)", types.GenerateContentConfig(response_mime_type="application/json")),
    ]
    ok = True
    for name, cfg in tests:
        try:
            r = client.models.generate_content(model=settings.gemini_model,
                                               contents='Reply with {"ok": true}', config=cfg)
            print(f"\n[pass] {name}: {(r.text or '').strip()[:60]}")
        except Exception as e:  # show Gemini's own reason
            ok = False
            print(f"\n[fail] {name}: {getattr(e, 'code', '')} {getattr(e, 'status', '')}")
            print(f"       {getattr(e, 'message', None) or e}")

    if not ok:
        try:
            names = [m.name.removeprefix("models/") for m in client.models.list()
                     if "flash" in m.name or "pro" in m.name]
            print("\nModels this key can use:\n  " + "\n  ".join(sorted(names)[:30]))
        except Exception as e:
            print(f"\nCould not list models: {getattr(e, 'message', None) or e}")
    print("\nAll good: restart the app and click Refresh brief." if ok else "")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
