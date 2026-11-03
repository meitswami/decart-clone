"""A/B: the same try-ons with the old parser (fashn-human-parser) and the new
MediaPipe parser. Run once per mode in a fresh process (the stand-in package
must be on PYTHONPATH from the start):

  python scripts/ab_parser.py fashn OUT_DIR      # PYTHONPATH without shims
  python scripts/ab_parser.py mediapipe OUT_DIR  # PYTHONPATH=shims:...
"""
import json, os, sys, time, urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
mode, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
SITE = "https://live-virtual-try-on.brandzaha.com/garments/"
FX = "https://raw.githubusercontent.com/fashn-AI/fashn-vton-1.5/main/examples/data/"
PAIRS = [  # person, garment, category
    (SITE + "black-floral-kurta.jpg", SITE + "pink-peacock-kurta-dhoti.jpg", "one-pieces"),
    (SITE + "olive-cutwork-suit.jpg", SITE + "yellow-silk-saree.jpg", "one-pieces"),
    (SITE + "yellow-silk-saree.jpg", SITE + "olive-cutwork-suit.jpg", "one-pieces"),
    (SITE + "yellow-skirt-set-pink-cape.jpg", SITE + "pink-floral-sharara-set.jpg", "one-pieces"),
    (SITE + "peach-printed-kurta-set.jpg", SITE + "white-henley-shirt.jpg", "tops"),
    (SITE + "navy-angrakha-dhoti.jpg", SITE + "black-kurta-red-panel.jpg", "one-pieces"),
]
H = {"User-Agent": "vto-ab/1.0"}
get = lambda u: urllib.request.urlopen(urllib.request.Request(u, headers=H), timeout=60).read()

from pipeline import photo_tryon  # noqa: E402
t = time.time()
photo_tryon.load(); photo_tryon.load_upscaler(); parser = photo_tryon.load_parser()
print(mode, "parser:", type(parser).__module__, "loaded in", round(time.time() - t, 1), "s", flush=True)
res = []
for i, (p, g, cat) in enumerate(PAIRS):
    try:
        img, info = photo_tryon.run_local(get(p), get(g), cat)
        open(f"{out}/{mode}_{i}.jpg", "wb").write(img)
        res.append({"i": i, "s": info.get("total_s"), "crop": info.get("person_crop"), "mask": info.get("mask"),
                    "gpt": info.get("garment_photo_type")})
    except Exception as e:
        res.append({"i": i, "error": repr(e)})
    print(res[-1], flush=True)
json.dump(res, open(f"{out}/{mode}.json", "w"), indent=1)
print("AB_DONE", mode, flush=True)
