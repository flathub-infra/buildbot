This is a version of buildbot with changes applied to make it work
for flathub.

To hack on this, run once:
  make virtualenv
  . .venv/bin/activate
  pip install psycopg2
  make frontend

When doing frontend work you can do that live with:
  cd www/base
  gulp dev

Then each time you can
  . .venv/bin/activate
  cd $master-dir
  buildbot start

Releases are done with:
  REL=...new release number..
  git tag v2.3.1.flathub${REL}
  git push origin v2.3.1.flathub${REL}
  make tarballs
Then upload the files from dist/*.whl
