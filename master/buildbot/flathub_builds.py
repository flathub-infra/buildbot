#!/usr/bin/env python2

from future.utils import string_types

import subprocess, re, json
import os.path
import utils

def id_is_valid(possible_id):
    return len(possible_id.split('.')) >= 3

def build_name_to_id(buildname):
    return buildname.split("/", 1)[0]

def strip_dot_git(module):
    if module.endswith(".git"):
        module = module[:-4]
    return module

def git_modules_equal(module_a, module_b):
    return strip_dot_git (module_a) == strip_dot_git (module_b)

class BuildDataRepo:
    def __init__(self, name, json):
        self.name = name
        self.base = json["base"]
        self.default_branch = json.get("default-branch", u"master")
        self.alternatives = json.get("alternatives", [])

class BuildDataInfo:
    def __init__(self, buildname, json, repos):
        self.buildname = buildname
        self.reponame = json.get("repo", "default")
        self.repo = repos[self.reponame]
        self.git_module = json.get("git-module", None)
        self.git_branch = json.get("git-branch", None)
        self.only_arches = json.get("only-arches", None)
        self.custom_buildcmd = json.get("custom-buildcmd", False)
        self.extra_prefixes = json.get("extra-prefixes", [])
        self.base = json.get("base", None)

    def get_git_branch(self):
        if self.git_branch:
            return self.git_branch
        return self.repo.default_branch

    def get_git_module(self):
        if self.git_module:
            return self.git_module
        id = build_name_to_id(self.buildname)
        return "%s.git" % id

# official, id, url and git_branch are non-optional
class BuildData:
    def __init__(self, id, fp_branch):
        self.id = id
        self.fp_branch = fp_branch
        self.url = None
        self.git_branch = None
        self.official = False
        self.custom_buildcmd = False
        self.extra_prefixes = []
        self.only_arches = None

    def get_name(self):
        if self.fp_branch is not None:
            return self.id + "/" + self.fp_branch
        return self.id

    # This might not be the same as the on in the data, as it may be
    # e.g. a test build from another repo. But, this is the official
    # location for this build and can be used for permission checks.
    def get_flathub_repo_uri(self):
        return "https://github.com/flathub/%s.git" % (self.id)

    def get_change(self, force_test=False, force_arches=None, untrusted=False):
        properties = {
            'flathub_id': self.id,
            'flathub_branch': self.fp_branch,
            'flathub_official_build': self.official and not force_test,
            'flathub_custom_buildcmd': self.custom_buildcmd,
            'flathub_extra_prefixes': self.extra_prefixes #",".join(self.extra_prefixes)
        }
        if force_arches:
            properties['flathub_arches'] = self.force_arches
        elif self.only_arches:
            properties['flathub_arches'] = self.only_arches
        if untrusted:
            properties['flathub_untrusted'] = True

        return {'repository': self.url,
                'branch': self.git_branch,
                'properties': properties}

    def get_json_manifest(self):
        return "%s.json" % self.id

    def get_yaml_manifest(self):
        return "%s.yaml" % self.id

    def get_yml_manifest(self):
        return "%s.yml" % self.id

    def __str__(self):
        return "%s %s %s %s %s" % (self.id, self.fp_branch, self.url, self.git_branch, "official" if self.official else "test")

class Builds:
    def __init__(self, filename):
        self.repos = {}
        self.builds = {}
        self.imports = {}
        self.default_repo = None
        self.by_base_uri = {}
        self.reverse_deps = {}
        self.reverse_repo_base = {}

        f = open(filename, 'r')
        config = utils.json_to_ascii(json.loads(f.read ()))
        for k in config["repos"]:
            r = BuildDataRepo(k, config["repos"][k])
            self.repos[k] = r
            for alt_base in r.alternatives:
                self.reverse_repo_base[alt_base] = r.base
        for k in config["builds"]:
            i = BuildDataInfo(k, config["builds"][k], self.repos)
            self.builds[k] = i
            if i.base:
                l = self.reverse_deps.get(i.base, [])
                l.append(i.buildname)
                self.reverse_deps[i.base] = l

        self.default_repo = self.repos["default"]
        for n in self.repos:
            r = self.repos[n]
            self.by_base_uri[r.base] = r
        self.imports = config["imports"]

    def get_canonical_url(self, alt_url):
        s = alt_url.rsplit('/', 1);
        base = s[0]
        git_module = s[1]
        if base in self.reverse_repo_base:
            return self.reverse_repo_base[base] + "/" + git_module
        return alt_url

    def reverse_dependency_lookup(self, buildname):
        res = []
        for id in self.reverse_deps.get(buildname, []):
            res.append(self.lookup_by_name(id))
        return res

    def lookup_by_git_and_name(self, repo_uri, git_branch, buildname):
        if repo_uri:
            if '/' in buildname:
                raise Exception("Can't specify non-empty version and custom git repo at the same time.")
            # Here, buildname is now always just an id
            data = self.lookup_by_git(repo_uri, git_branch if git_branch else u'master', buildname)
            # If we specified a custom git uri then the git module name might not be the
            # app id, so in this case we need to remember the actual app id that
            # the user specified
        elif buildname:
            data = self.lookup_by_name(buildname)
            if git_branch:
                data.git_branch = git_branch
                # We're overriding the branch, so we loose officialness
                data.official = False
        else:
            raise Exception("Must specify either repo uri or buildname")
        return data

    def lookup_by_name(self, buildname):
        split = buildname.split("/", 1)
        id = split[0]
        fp_branch = None
        if len(split) > 1:
            fp_branch = split[1]

        extra_prefixes = []
        custom_buildcmd = False
        only_arches = None

        if self.builds.has_key(buildname):
            info = self.builds[buildname]
            repo = info.repo
            module = info.get_git_module()
            git_branch = info.get_git_branch()
            custom_buildcmd = info.custom_buildcmd
            only_arches = info.only_arches
            extra_prefixes = info.extra_prefixes
        else: # No build, create default
            if self.id_used_in_buildname(id):
                raise Exception("No defined build %s" % buildname)
            repo = self.default_repo
            module = "%s.git" % id
            if fp_branch == None:
                git_branch = repo.default_branch
            else:
                git_branch = "branch/" + fp_branch;
            if not id_is_valid(id):
                raise Exception("Invalid build name %s" % buildname)

        d = BuildData(id, fp_branch)
        d.url = "%s/%s" % (repo.base, module)
        d.git_branch = git_branch
        d.official = True
        d.custom_buildcmd = custom_buildcmd
        d.only_arches = only_arches
        d.extra_prefixes = extra_prefixes
        return d

    def find_build_by_repo_module_branch(self, repo, git_module, git_branch):
        for b_name in self.builds:
            info = self.builds[b_name]
            # Match repo (base uri)
            if repo.name != info.reponame:
                continue
            # Match module name
            if not git_modules_equal (git_module, info.get_git_module()):
                continue
            # Match git branch
            if git_branch != info.get_git_branch():
                continue
            return b_name
        return None

    def id_used_in_buildname(self, id):
        for b_name in self.builds:
            if id == build_name_to_id(b_name):
                return True
        return False

    # The id here is optional, its never passed when
    # we get git change notifications, but it may
    # be optionally set by the user for test builds
    # because in that case the app id may differ
    # from the repo name (git module)
    def lookup_by_git(self, git_url, git_branch, optional_id=None):
        if optional_id and '/' in optional_id:
            raise Exception("Invalid id %s, slash not allowed" % optional_id)

        git_url = self.get_canonical_url(git_url)
        s = git_url.rsplit('/', 1);
        base = s[0]
        git_module = s[1]

        if optional_id:
            id = optional_id
        else:
            id = strip_dot_git(git_module)

        repo = self.by_base_uri.get(base, None)
        if repo:
            # Maybe it exactly matches a specified build name (and id if specified)
            exact_match = self.find_build_by_repo_module_branch(repo, git_module, git_branch)
            if exact_match and (not optional_id or optional_id == build_name_to_id(exact_match)):
                return self.lookup_by_name(exact_match)

            # It could be a default build name from the git module if:
            # * It matches the default repo
            # * It matches the default branch in the default repo, or is the form branch/$fp_branch
            # * The git module name matches the id
            # * And there is no explicit buildnames with that id as prefix (so, e.g no
            #    default "org.kde.Sdk" if a "org.kde.Sdk/5.9" is configured, we only allow
            #   exact matches in that case)
            default_git_branch = self.default_repo.default_branch
            if (id_is_valid(id) and
                repo == self.default_repo and
                git_modules_equal(id, git_module) and
                not self.id_used_in_buildname(id)):
                name = None
                if  git_branch == default_git_branch:
                    name = id;
                elif git_branch.startswith("branch/"):
                    name = id + "/" + git_branch[7:];
                if name != None:
                    return self.lookup_by_name(name)

        # Make sure the module is a valid name
        if not id_is_valid(id):
            raise Exception("Invalid id %s" % id)

        # Make up a "fake" BuildData for test builds
        fake = BuildData(id, None)
        fake.url = git_url
        fake.git_branch = git_branch

        return fake

# Tests:

def verify_by_name_exception(buildname):
    global test_builds
    print("testing lookup_by_name(%s)" % buildname)
    try:
        r = test_builds.lookup_by_name(buildname)
    except Exception as e:
        return
    raise Exception("No exception for buildname %s when one was expected" % buildname)

def verify_by_name(buildname, id = None, url = None, git_branch = None, fp_branch = None):
    global test_builds
    if id == None:
        id = build_name_to_id(buildname)
    if url == None:
        url = "https://github.com/flathub/%s.git" % id
    if git_branch == None:
        git_branch = "master"

    print("testing lookup_by_name(%s)" % buildname)
    try:
        res = test_builds.lookup_by_name(buildname)
    except Exception as e:
        raise Exception("Got unexpected exception %s buildname %s" % (e, buildname))
    if not res.official:
        raise Exception("Got non-official result from lookup_by_name %s" % (buildname))
    if id != res.id:
        raise Exception("Got unexpected id %s (expected %s) from buildname %s" % (res.id, id, buildname))
    if url != res.url:
        raise Exception("Got unexpected url %s (expected %s) from buildname %s" % (res.url, url, buildname))
    if git_branch != res.git_branch:
        raise Exception("Got unexpected git_branch %s (expected %s) from buildname %s" % (res.git_branch, git_branch, buildname))
    if fp_branch != res.fp_branch:
        raise Exception("Got unexpected fp_branch %s (expected %s) from buildname %s" % (res.fp_branch, fp_branch, buildname))

def test_lookup_by_name():
    # Regular app
    verify_by_name("org.app.regular")
    # Branch override
    verify_by_name("org.app.special-branch", git_branch="special-branch")
    # Repo override
    verify_by_name("org.app.special-repo", url="https://github.com/special/org.app.special-repo.git")
    # Repo override w/ custom default branch
    verify_by_name("org.app.special-repo2", url="https://github.com/special2/org.app.special-repo2.git", git_branch="special2")
    # Repo override w/ custom default branch, overriding branch
    verify_by_name("org.app.special-repo2-branch", url="https://github.com/special2/org.app.special-repo2-branch.git", git_branch="override")
    # Regular extra-fp-branch
    verify_by_name("org.app.has-version/1.0", fp_branch="1.0", git_branch="1-0")
    # Non-configured special branch
    verify_by_name("org.app.regular/foobar", fp_branch="foobar", git_branch="branch/foobar")
    # Don't allow non-versioned
    verify_by_name_exception("org.app.has-version")
    # Don't allow non-specified version
    verify_by_name_exception("org.app.has-version/2.0")
    # Override git module
    verify_by_name("org.app.special-module", url="https://github.com/flathub/special-module.git")

    # Complex combinations
    verify_by_name_exception("org.kde.Sdk")
    verify_by_name_exception("org.kde.Sdk/5.8")
    verify_by_name("org.kde.Sdk/5.9", fp_branch="5.9", url="https://github.com/KDE/org.kde.Sdk.git", git_branch="kde-5-9")
    verify_by_name("org.kde.Sdk/5.10", fp_branch="5.10", url="https://github.com/KDE2/flatpak-kde-runtime.git", git_branch="kde-branch")
    verify_by_name("org.kde.Sdk/5.11", fp_branch="5.11", url="https://github.com/KDE2/flatpak-kde-runtime.git", git_branch="kde-5-11")

def verify_by_git_exception(url, git_branch, optional_id = None):
    global test_builds
    print("testing lookup_by_git(%s, %s, %s)" % (url, git_branch, optional_id))
    try:
        r = test_builds.lookup_by_git(url, git_branch, optional_id)
    except Exception as e:
        return
    raise Exception("No exception for git lookup when one was expected")

def verify_by_git(url_arg, git_branch, id, optional_id = None,
                  official = False, url = None, fp_branch = None):
    global test_builds
    if url == None:
        url = url_arg
    print("testing lookup_by_git(%s, %s, %s)" % (url_arg, git_branch, optional_id))
    try:
        res = test_builds.lookup_by_git(url_arg, git_branch, optional_id)
    except Exception as e:
        raise Exception("Got unexpected exception '%s' when looking up by git" % e)
    if url != res.url:
        raise Exception("Got unexpected url %s (expected %s)" % (url, res.url))
    if git_branch != res.git_branch:
        raise Exception("Got unexpected git branch %s (expected %s)" % (git_branch, res.git_branch))
    if id != res.id:
        raise Exception("Got unexpected id %s (expected %s)" % (official, res.id))
    if official != res.official:
        raise Exception("Got unexpected official %s (expected %s)" % (official, res.official))
    if fp_branch != res.fp_branch:
        raise Exception("Got unexpected fp_branch %s (expected %s)" % (fp_branch, res.fp_branch))

def verify_by_git_test(url_arg, git_branch, id, optional_id = None,
                           url = None, fp_branch = None):
    return verify_by_git(url_arg, git_branch, id, optional_id = optional_id, url=url, fp_branch=fp_branch, official=False)

def verify_by_git_official(url_arg, id, git_branch = "master", optional_id = None,
                           url = None, fp_branch = None):
    return verify_by_git(url_arg, git_branch, id, optional_id = optional_id, url=url, fp_branch=fp_branch, official=True)

def test_lookup_by_git():
    # Random uris should generate test builds
    verify_by_git_test("https://random.com/org.app.regular.git", "master", "org.app.regular")
    verify_by_git_test("https://random.com/org.app.regular", "master", "org.app.regular")
    verify_by_git_test("https://random.com/org.app.regular.git", "master", "org.app.regular", optional_id = "org.app.regular")
    verify_by_git_test("https://random.com/org.app.regular.git", "master", "org.app.regular2", optional_id = "org.app.regular2")
    verify_by_git_test("https://random.com/org.app.regular", "master", "org.app.regular", optional_id = "org.app.regular")
    verify_by_git_test("https://random.com/org.app.regular", "master", "org.app.regular2", optional_id = "org.app.regular2")
    verify_by_git_test("https://random.com/foobar.git", "master", "org.app.regular3", optional_id = "org.app.regular3")
    verify_by_git_test("https://random.com/org.app.regular.git", "other-branch", "org.app.regular")


    # Weird (non-id) uris should fail if you don't specify an id
    verify_by_git_exception("https://random.com/foobar.git", "master")
    verify_by_git_exception("https://github.com/flathub/flathub.org.git", "master")
    verify_by_git_exception("https://github.com/flathub/flathub.org", "master")

    # Invalid ids should fail
    verify_by_git_exception("https://random.com/foobar.git", "master", "org.foo")
    verify_by_git_exception("https://random.com/foobar.git", "master", "org.foo.bar/1.2")

    # Regular uris with weird branch should generate test builds
    verify_by_git_test("https://github.com/flathub/org.app.regular.git", "test", "org.app.regular")
    verify_by_git_test("https://github.com/flathub/org.app.regular", "test", "org.app.regular")

    # Regular default (non-configured) official buils
    verify_by_git_official("https://github.com/flathub/org.app.regular.git", "org.app.regular")
    verify_by_git_official("https://github.com/flathub/org.app.regular", "org.app.regular",
                           url="https://github.com/flathub/org.app.regular.git")

    # If a special branch is configured, we should match an official build
    verify_by_git_official("https://github.com/flathub/org.app.special-branch.git", "org.app.special-branch", git_branch="special-branch")
    # Or we use a git branch named "branch/*" branches:
    verify_by_git_official("https://github.com/flathub/org.app.regular.git", "org.app.regular", git_branch="branch/foobar", fp_branch="foobar")
    verify_by_git_official("https://github.com/flathub/org.app.regular.git", "org.app.regular", git_branch="branch/some-other", fp_branch="some-other")
    # For other branches (including master), we shold get a test build
    verify_by_git_test("https://github.com/flathub/org.app.special-branch.git", "master", "org.app.special-branch")

    # Some non-standard repos are official (and may require specific branches)
    verify_by_git_official("https://github.com/special/org.app.special-repo.git", "org.app.special-repo")
    verify_by_git_official("https://github.com/special/org.app.special-repo", "org.app.special-repo", url="https://github.com/special/org.app.special-repo.git")
    verify_by_git_official("https://github.com/special2/org.app.special-repo2.git", "org.app.special-repo2", git_branch="special2")
    verify_by_git_official("https://github.com/special2/org.app.special-repo2", "org.app.special-repo2", git_branch="special2", url="https://github.com/special2/org.app.special-repo2.git")
    verify_by_git_official("https://github.com/special2/org.app.special-repo2-branch.git", "org.app.special-repo2-branch", git_branch="override")
    verify_by_git_official("https://github.com/special2/org.app.special-repo2-branch", "org.app.special-repo2-branch", git_branch="override", url="https://github.com/special2/org.app.special-repo2-branch.git")
    # When in other locations / branches they are not:
    verify_by_git_test("https://github.com/nonspecial/org.app.special-repo.git", "master", "org.app.special-repo")
    verify_by_git_test("https://github.com/special2/org.app.special-repo2.git", "master", "org.app.special-repo2")
    verify_by_git_test("https://github.com/special2/org.app.special-repo2", "master", "org.app.special-repo2")
    verify_by_git_test("https://github.com/special2/org.app.special-repo2-branch.git", "master", "org.app.special-repo2-branch")
    verify_by_git_test("https://github.com/special2/org.app.special-repo2-branch", "master", "org.app.special-repo2-branch")
    verify_by_git_test("https://github.com/special2/org.app.special-repo2-branch.git", "special2", "org.app.special-repo2-branch")
    verify_by_git_test("https://github.com/special2/org.app.special-repo2-branch", "special2", "org.app.special-repo2-branch")

    # Test Versioned buildnames with right branch
    verify_by_git_official("https://github.com/flathub/org.app.has-version.git", "org.app.has-version", git_branch="1-0", fp_branch="1.0")
    verify_by_git_official("https://github.com/flathub/org.app.has-version", "org.app.has-version", git_branch="1-0", fp_branch="1.0",
                           url="https://github.com/flathub/org.app.has-version.git")
    # But not with wrong branch
    verify_by_git_test("https://github.com/flathub/org.app.has-version.git", "master", "org.app.has-version")
    verify_by_git_test("https://github.com/flathub/org.app.has-version", "master", "org.app.has-version")

    # Some official apps have special module names
    verify_by_git_official("https://github.com/flathub/special-module.git", "org.app.special-module")
    verify_by_git_official("https://github.com/flathub/special-module", "org.app.special-module",
                           url="https://github.com/flathub/special-module.git")
    # Such apps are not official builds in the "regular" name
    verify_by_git_test("https://github.com/flathub/org.app.special-module.git", "master", "org.app.special-module")
    verify_by_git_test("https://github.com/flathub/org.app.special-module", "master", "org.app.special-module")

    # Complex combinations
    verify_by_git_test("https://github.com/KDE/org.kde.Sdk.git", "kde-5-8", "org.kde.Sdk") # No 5.8 configured
    verify_by_git_test("https://github.com/KDE/org.kde.Other.git", "kde-5-9", "org.kde.Other") # No Other configured
    verify_by_git_official("https://github.com/KDE/org.kde.Sdk.git", "org.kde.Sdk", git_branch="kde-5-9", fp_branch="5.9")
    verify_by_git_official("https://github.com/KDE/org.kde.Sdk", "org.kde.Sdk", git_branch="kde-5-9", fp_branch="5.9",
                           url="https://github.com/KDE/org.kde.Sdk.git")
    verify_by_git_official("https://github.com/KDE/org.kde.Sdk.git", "org.kde.Sdk", git_branch="kde-5-9", fp_branch="5.9")
    verify_by_git_official("https://github.com/KDE2/flatpak-kde-runtime.git", "org.kde.Sdk", git_branch="kde-branch", fp_branch="5.10")
    verify_by_git_official("https://github.com/KDE2/flatpak-kde-runtime", "org.kde.Sdk", git_branch="kde-branch", fp_branch="5.10",
                           url="https://github.com/KDE2/flatpak-kde-runtime.git")
    verify_by_git_official("https://github.com/KDE2/flatpak-kde-runtime.git", "org.kde.Sdk", git_branch="kde-5-11", fp_branch="5.11")
    verify_by_git_official("https://github.com/KDE2/flatpak-kde-runtime", "org.kde.Sdk", git_branch="kde-5-11", fp_branch="5.11",
                           url="https://github.com/KDE2/flatpak-kde-runtime.git")
    verify_by_git_test("https://github.com/KDE2/org.kde.Sdk.git", "kde-branch", "org.kde.Sdk")

test_builds = None
if __name__ == "__main__":
    test_builds = Builds('builds-test.json')

    test_lookup_by_name()
    test_lookup_by_git()
