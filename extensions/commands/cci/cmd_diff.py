import json
import os

import requests
import subprocess
from conan.api.model import ListPattern, RecipeReference
from conan.cli.command import conan_command
from conan.errors import ConanException
from conan.cli.formatters.report import format_diff_html, format_diff_txt, format_diff_json


def output_json(results):
    print(json.dumps(results, indent=2))


@conan_command(group="Conan Center Index",formatters={"text": format_diff_txt,
                              "json": format_diff_json,
                              "html": format_diff_html})
def diff(conan_api, parser, *args):
    """
    Diffs the contents of a new-version PR against the current latest version of the Conan Center Index.
    """
    parser.add_argument("pr_number", type=int, help="The PR number to diff against the latest version.")
    parser.add_argument("name", help="The name of the recipe to diff, e.g., 'zlib'.")
    parser.add_argument("local_repo_name", help="The name of the local index repository, e.g., 'conan-center-index'.",)

    args = parser.parse_args(*args)
    pr_number = args.pr_number
    local_repo_name = args.local_repo_name

    # Get list of references in the local repository
    ref_pattern = ListPattern(f"{args.name}/*", rrev=None, prev=None)

    local_remote = conan_api.remotes.get(local_repo_name)


    # Add a new remote to fetch the patch from the PR
    api_response = requests.get(f"https://api.github.com/repos/conan-io/conan-center-index/pulls/{pr_number}")
    if api_response.status_code != 200:
        raise ConanException(f"Failed to fetch patch for PR info #{pr_number}: {api_response.status_code} {api_response.reason}")
    result = api_response.json()
    base_sha = result["base"]["sha"]
    head_repo_id = str(result["head"]["repo"]["id"])
    head_ref = result["head"]["ref"]
    head_clone_url = result["head"]["repo"]["clone_url"]

    try:
        # Ensure the local repository is checked out to the base commit of the PR
        subprocess.run(["git", "checkout", base_sha], cwd=local_remote.url, check=True)
        old_ref_list = conan_api.list.select(ref_pattern, None, local_remote).serialize()

        # Clone the PR branch into the local repository
        try:
            subprocess.run(["git", "remote", "show", head_repo_id], cwd=local_remote.url, check=True,
                           stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            subprocess.run(["git", "remote", "add", head_repo_id, head_clone_url], cwd=local_remote.url, check=True)
        subprocess.run(["git", "fetch", head_repo_id, head_ref], cwd=local_remote.url, check=True)
        subprocess.run(["git", "checkout", head_ref], cwd=local_remote.url, check=True)

        local_remote.invalidate_cache()
        # Get the list of references after applying the patch
        new_ref_list = conan_api.list.select(ref_pattern, None, local_remote).serialize()

        # Compare the old and new reference lists
        new_refs = set(new_ref_list) - set(old_ref_list)
        # There should only be 1 new reference, the one that was added by the PR
        if len(new_refs) != 1:
            raise ConanException(f"Expected exactly one new reference, found {len(new_refs)}: {new_refs}")
        new_ref_str = new_refs.pop()
        new_ref = RecipeReference.loads(new_ref_str)

        old_refs = sorted([ref for ref in
                    [RecipeReference.loads(ref) for ref in old_ref_list if ref != new_ref_str]
                    if ref.name == new_ref.name])
        if not old_refs:
            raise ConanException(f"No previous version found for {new_ref.name}.")
        old_ref = old_refs[-1]  # Get the latest version of the reference

        # Now call the diff command
        result = conan_api.command.run(f"report diff -or={old_ref} -nr={new_ref_str} -r={local_repo_name}")
        return result
    finally:
        subprocess.run(["git", "checkout", "master"], cwd=local_remote.url)







