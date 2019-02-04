#!/bin/bash

shopt -s nullglob

echo Free space on build disk:
df -h .

rm -frv ~/.cache/flatpak
rm -frv ~/.local/share/flatpak/repo/tmp/cache

du -csh ../../*
for i in ../../build-*; do
    if test -d $i; then
         pushd $i
         echo Scanning `basename $i`
         du -ha -d2
         for i in */.flatpak-builder/rofiles/rofiles-*; do
             fusermount -u -z $i || true
         done
         find -mindepth 1 -maxdepth 1 -type d -print0 | xargs -0t rm -rf
         popd
    fi
done

echo Free space on build disk:
df -h .
