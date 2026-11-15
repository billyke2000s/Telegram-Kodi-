#!/usr/bin/env python3
"""Build the Kodi repository served by GitHub Pages from the docs/ folder.

Run from the repo root after changing an add-on (bump its version in addon.xml first,
otherwise Kodi boxes won't see an update):

    python3 tools/build_repo.py

Output (docs/):
    index.html                      directory listing Kodi's File Manager can browse
    repository.telegramkodi-X.zip   the zip users install from that listing
    addons.xml / addons.xml.md5     index Kodi checks for updates
    <addon id>/<addon id>-<ver>.zip one zip per add-on
"""
import hashlib
import os
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs")
ADDONS = ["repository.telegramkodi", "plugin.video.telegram"]
SKIP_DIRS = {"__pycache__", ".git"}
SKIP_EXT = {".pyc", ".pyo"}


def addon_info(addon_dir):
    root = ET.parse(os.path.join(addon_dir, "addon.xml")).getroot()
    return root.get("id"), root.get("version")


def make_zip(addon_dir, addon_id, version):
    dest_dir = os.path.join(OUT, addon_id)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{addon_id}-{version}.zip")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, dirs, files in os.walk(addon_dir):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                if os.path.splitext(name)[1] in SKIP_EXT:
                    continue
                full = os.path.join(base, name)
                arc = os.path.join(addon_id, os.path.relpath(full, addon_dir))
                zf.write(full, arc.replace(os.sep, "/"))
    for extra in ("icon.png", "fanart.jpg"):
        src = os.path.join(addon_dir, extra)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest_dir, extra))
    return dest


def main():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    xml_parts = []
    repo_zip_name = None
    for addon_id_dir in ADDONS:
        addon_dir = os.path.join(ROOT, addon_id_dir)
        addon_id, version = addon_info(addon_dir)
        zip_path = make_zip(addon_dir, addon_id, version)
        print("built", os.path.relpath(zip_path, ROOT))

        with open(os.path.join(addon_dir, "addon.xml"), encoding="utf-8") as f:
            text = f.read()
        text = re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", text)
        xml_parts.append(text.strip())

        if addon_id.startswith("repository."):
            repo_zip_name = os.path.basename(zip_path)
            shutil.copy2(zip_path, os.path.join(OUT, repo_zip_name))

    addons_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n'
                  + "\n\n".join(xml_parts) + "\n</addons>\n")
    with open(os.path.join(OUT, "addons.xml"), "w", encoding="utf-8") as f:
        f.write(addons_xml)
    with open(os.path.join(OUT, "addons.xml.md5"), "w") as f:
        f.write(hashlib.md5(addons_xml.encode("utf-8")).hexdigest())

    # Kodi's File Manager reads plain <a href> links from an HTML page.
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write("<!DOCTYPE html>\n<html><body>\n"
                f'<a href="{repo_zip_name}">{repo_zip_name}</a>\n'
                "</body></html>\n")
    # Stop GitHub Pages running Jekyll over the files.
    open(os.path.join(OUT, ".nojekyll"), "w").close()
    print("done:", os.path.relpath(OUT, ROOT))


if __name__ == "__main__":
    main()
