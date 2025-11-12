import os
import shutil

from conan.api.conan_api import ConanAPI
from conan.cli.command import conan_command, OnceArgument
from conan.cli.args import add_lockfile_args, add_common_install_arguments
from conan.cli.formatters.graph import format_graph_json
from conan.cli.printers import print_profiles
from conan.cli.commands.test import run_test
from conan.api.model import RecipeReference


@conan_command(group="Creator", formatters={"json": format_graph_json})
def test_pkg_info(conan_api: ConanAPI, parser, *args):
    """
    Test a package from a test_package folder.
    """
    parser.add_argument("path", action=OnceArgument,
                        help="Path to a test_package folder containing a conanfile.py")
    parser.add_argument("reference", action=OnceArgument,
                        help='Provide a package reference to test')
    parser.add_argument("recipe", action=OnceArgument,
                        help='Provide the path to the recipe to be tested')
    add_common_install_arguments(parser)
    add_lockfile_args(parser)
    args = parser.parse_args(*args)

    cwd = os.getcwd()
    ref = RecipeReference.loads(args.reference)
    path = conan_api.local.get_conanfile_path(args.path, cwd, py=True)
    overrides = eval(args.lockfile_overrides) if args.lockfile_overrides else None
    lockfile = conan_api.lockfile.get_lockfile(lockfile=args.lockfile,
                                               conanfile_path=path,
                                               cwd=cwd,
                                               partial=args.lockfile_partial,
                                               overrides=overrides)
    remotes = conan_api.remotes.list(args.remote) if not args.no_remote else []
    profile_host, profile_build = conan_api.profiles.get_profiles_from_args(args)

    print_profiles(profile_host, profile_build)

    recipe_path = conan_api.local.get_conanfile_path(args.recipe, cwd, py=True)
    # get path for latest revision of the recipe in cache which will be used in the test
    latest_ref = conan_api.list.latest_recipe_revision(ref, None)
    export_path = conan_api.cache.export_path(latest_ref)
    cache_path = conan_api.local.get_conanfile_path(export_path, cwd, py=True)
    backup_recipe_path = cache_path + ".bak"
    # replace the recipe in cache by the one provided
    shutil.copyfile(src=cache_path, dst=backup_recipe_path)
    shutil.copyfile(src=recipe_path, dst=cache_path)

    deps_graph = run_test(conan_api, path, ref, profile_host, profile_build, remotes, lockfile,
                          args.update, build_modes=args.build, tested_python_requires=ref)

    # restore the original recipe in cache
    shutil.copyfile(src=backup_recipe_path, dst=export_path)
    os.remove(backup_recipe_path)

    lockfile = conan_api.lockfile.update_lockfile(lockfile, deps_graph, args.lockfile_packages,
                                                  clean=args.lockfile_clean)
    conan_api.lockfile.save_lockfile(lockfile, args.lockfile_out, cwd)

    return {"graph": deps_graph,
            "conan_api": conan_api}
