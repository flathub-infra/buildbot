#!/bin/bash

set -e
shopt -s nullglob

SOURCEDIR="$1"
mkdir -p $SOURCEDIR/downloads
mkdir -p $SOURCEDIR/git
mkdir -p $SOURCEDIR/bzr

if test -d .flatpak-builder/downloads/; then
    cp -rnv .flatpak-builder/downloads $SOURCEDIR
fi

for repo in .flatpak-builder/git/*; do
    mv -nvT $repo $SOURCEDIR/git/`basename $repo`
done

for repo in .flatpak-builder/bzr/*; do
    mv -nvT $repo $SOURCEDIR/bzr/`basename $repo`
done
