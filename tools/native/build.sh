#!/bin/sh
# Builds the freestanding IGE helper for Android/Linux boxes. Needs clang + lld.
# Output goes into the add-on: resources/lib/_native/ige/<arch>/libtgige.so
set -e
cd "$(dirname "$0")"
OUT=../../plugin.video.telegram/resources/lib/_native/ige
COMMON="-O2 -fPIC -ffreestanding -fno-builtin -fno-stack-protector -nostdlib -shared -fuse-ld=lld \
  -fvisibility=hidden -Wl,-soname,libtgige.so -Wl,--hash-style=both -Wl,-z,noexecstack \
  -Wl,-z,max-page-size=16384 -Wl,--build-id=none"
build() {  # arch target [extra flags]
  mkdir -p "$OUT/$1"
  clang --target="$2" $3 $COMMON -o "$OUT/$1/libtgige.so" tgige.c
  echo "built $1"
}
build arm64  aarch64-linux-android21
build arm    armv7a-linux-androideabi21 "-mthumb -mfloat-abi=softfp"
build x86_64 x86_64-linux-android21
build x86    i686-linux-android21
