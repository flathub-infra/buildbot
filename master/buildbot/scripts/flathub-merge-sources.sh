#!/bin/bash

set -e
shopt -s nullglob

SOURCEDIR="$1"
mkdir -p $SOURCEDIR/downloads

if test -d .flatpak-builder/downloads/; then
    cp -rnv .flatpak-builder/downloads $SOURCEDIR
fi

for vcs in git bzr svn; do
    mkdir -p $SOURCEDIR/$vcs
    for repo in .flatpak-builder/git/*; do
        mv -nvT $repo $SOURCEDIR/$vcs/`basename $repo`
    done
done
