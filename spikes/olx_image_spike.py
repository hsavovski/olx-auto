"""Spike: does OLX copy images[].url to its CDN? (spec §9 open question). Findings: docs/spike-notes.md.

Posts ONE real advert on the given account with presigned S3 images, deletes the S3 objects once OLX
lists its own image URLs, checks those still load, polls status for moderation, then always deletes
the advert. Needs S3_* / AWS_* in .env (infra/s3-setup.sh).

    uv run --with boto3 python spikes/olx_image_spike.py [token_file] [max_wait_minutes]
"""
import io
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import boto3
from botocore.config import Config
from PIL import Image, ImageDraw

from olx_auto.config import ROOT, Settings, load_env
from olx_auto.olx import Olx, OlxError

OUT = Path(__file__).parent / "out"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def test_jpeg(color, label):
    img = Image.new("RGB", (1200, 900), color)
    ImageDraw.Draw(img).text((60, 60), label, fill="white", font_size=96)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def http_status(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.status, len(r.read())
    except urllib.error.HTTPError as e:
        return e.code, 0


def main():
    token_file = Path(sys.argv[1] if len(sys.argv) > 1 else "data/users/ico-test/olx_token.json")
    max_wait = 60 * float(sys.argv[2] if len(sys.argv) > 2 else 20)
    env, s = load_env(ROOT / ".env"), Settings.load()
    olx = Olx(token_file, s.olx_client_id, s.olx_client_secret)
    # SigV4 + regional endpoint: the default presigns for the global endpoint, which rejects non-us-east-1
    s3 = boto3.client("s3", region_name=env["AWS_REGION"],
                      aws_access_key_id=env["AWS_ACCESS_KEY_ID"],
                      aws_secret_access_key=env["AWS_SECRET_ACCESS_KEY"],
                      config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
    bucket = env["S3_BUCKET"]
    OUT.mkdir(exist_ok=True)

    keys, urls = [], []
    for color, label in [("#2a6f97", "TEST 1"), ("#9c2a2a", "TEST 2")]:
        key = f"spike/{uuid.uuid4().hex}.jpg"
        s3.put_object(Bucket=bucket, Key=key, Body=test_jpeg(color, label), ContentType="image/jpeg")
        keys.append(key)
        urls.append(s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key},
                                              ExpiresIn=7 * 24 * 3600))
    log("uploaded", keys, "presigned ok:", [http_status(u)[0] for u in urls])

    advert = olx.create_advert(
        title="Тестова обява - моля игнорирайте",
        description="Тестова обява за проверка на качването на снимки. Не е реален продукт и ще бъде "
                    "изтрита до няколко минути. Моля, не пишете.",
        category_id=185, advertiser_type="private", contact={"name": "Тест"},
        location={"city_id": 8771, "district_id": 1585},
        price={"value": 50, "currency": "EUR", "negotiable": False},
        attributes=[{"code": "state", "value": "used"}],
        images=[{"url": u} for u in urls],
    )
    aid = advert["id"]
    (OUT / "image_post_created.json").write_text(json.dumps(advert, ensure_ascii=False, indent=1))
    log("created", aid, advert["status"], "images:", [i["url"][:80] for i in advert["images"]])

    history, s3_deleted, cdn = [], False, []
    try:
        start = time.time()
        while time.time() - start < max_wait:
            a = olx.advert(aid)
            imgs = [i["url"] for i in a["images"]]
            if not history or history[-1][1] != a["status"]:
                history.append((round(time.time() - start), a["status"]))
                log("status", a["status"], "images:", [u[:80] for u in imgs])
            if not s3_deleted and imgs and all("olxcdn" in u for u in imgs):
                cdn = imgs
                log("OLX lists CDN images:", cdn)
                for k in keys:
                    s3.delete_object(Bucket=bucket, Key=k)
                s3_deleted = True
                log("S3 objects deleted; presigned now:", [http_status(u)[0] for u in urls])
                time.sleep(10)
                log("OLX CDN images after S3 delete:", [http_status(u) for u in cdn])
            # limited = free quota in this category used up (needs a paid package); it won't change on its own
            if a["status"] in ("active", "limited", "moderated", "blocked", "removed_by_moderator") and s3_deleted:
                break
            time.sleep(20)
        (OUT / "image_post_final.json").write_text(json.dumps(a, ensure_ascii=False, indent=1))
    finally:
        if not s3_deleted:
            for k in keys:
                s3.delete_object(Bucket=bucket, Key=k)
            log("S3 objects deleted (cleanup)")
        if olx.advert(aid)["status"] == "active":
            olx.advert_command(aid, "deactivate")
            log("deactivated")
        olx.delete_advert(aid)
        log("advert delete requested")
        time.sleep(15)
        try:
            log("advert still there:", olx.advert(aid)["status"])
        except OlxError as e:
            log("advert gone:", e.status)
        if cdn:
            log("OLX CDN images after advert delete:", [http_status(u) for u in cdn])
        log("status history (seconds, status):", history)


if __name__ == "__main__":
    main()
