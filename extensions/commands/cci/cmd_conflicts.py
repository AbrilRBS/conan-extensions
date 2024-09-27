import json
import os
from types import SimpleNamespace

import yaml
import copy
from conan.api.output import ConanOutput, cli_out_write
from conan.cli.command import conan_command
from conan.api.conan_api import ConanAPI
from conans.errors import ConanException
from conans.model.recipe_ref import RecipeReference
from conans.client.graph.graph_error import GraphConflictError


def format_conflicts(conflicts):
    cli_out_write(json.dumps(conflicts, indent=2))


@conan_command(group="Conan Center Index", formatters={"json": format_conflicts})
def conflicts(conan_api: ConanAPI, parser, *args):
    """Gets the list of packages that contain conflicts in their own graph
    """
    parser.add_argument("--repo-path", help="Path to the local conan center index clone", required=True)
    parser.add_argument("--profiles-path", help="Path to the profiles folder", required=True)
    parser.add_argument("--profiles-map", help="Path to the profiles map file", required=True)

    args = parser.parse_args(*args)
    out = ConanOutput()

    with open(args.profiles_map) as json_file:
        profile_map = yaml.safe_load(json_file)

    out.info(f"Loaded {len(profile_map['profiles'])} base profiles")

    list_output = conan_api.command.run("list *")

    if False and len(list_output["results"]["Local Cache"]) != 0:
        raise ConanException("The cache must be empty to run this command")

    #export_versions_output = conan_api.command.run(f"cci:export-all-versions -p {os.path.join(args.repo_path, 'recipes')}")

    #exported_list = export_versions_output["exported_with_versions"]

    exported_list = [
        "bear/3.0.21",
        "daw_json_link/3.24.1",
        "librasterlite/1.1g",
        "logr/0.1.0",
        "logr/0.6.0",
        "mbits-lngs/0.7.6",
        "openassetio/1.0.0-alpha.9",
        "opencascade/7.5.0",
        "opencascade/7.6.0",
        "opencascade/7.6.2",
        "openmvg/2.0",
        "qcustomplot/2.1.0",
        "qcustomplot/2.1.1",
        "samarium/1.0.1",
        "seadex-essentials/2.1.3",
        "seadex-genesis/2.0.0",
        "ulfius/2.7.11"
    ]


    exported_list = [RecipeReference(*ref.split("/")) for ref in exported_list]

    out.info(f"Exported {len(exported_list)} references")

    out.title("Check which packages have conflicts internally")

    remotes = conan_api.remotes.list(["conancenter"])

    conflicts = generate_conflicts(conan_api, exported_list, profile_map, args.profiles_path, remotes)

    if not conflicts:
        out.info("No packages need to be rebuilt")
        return

    return conflicts


def generate_conflicts(conan_api, reference_list, profile_map, profile_folder, remotes):
    # Result variables
    conflicts = {}
    out = ConanOutput()
    grouped_references = {}
    for reference in reference_list:
        grouped_references.setdefault(reference.name, []).append(reference)

    total_groups = len(grouped_references)
    for i, (group, references) in enumerate(grouped_references.items()):
        out.info(f"Checking group {i + 1}/{total_groups} of references ({group})")

        for reference in references:
            profiles = expand_profiles(conan_api, reference, profile_map)

            for profile_info in profiles:
                host_profile = profile_info['host_profile']
                build_profile = profile_info['build_profile']
                cppstd_values = profile_info['cppstd']

                options_host = profile_info.get('host_options', [])
                options_build = profile_info.get('build_options', [])
                settings_host = profile_info.get('host_settings', [])
                settings_build = profile_info.get('build_settings', [])
                conf_host = profile_info.get('host_conf', [])
                conf_build = profile_info.get('build_conf', [])
                continue_group = True
                for cppstd in cppstd_values:
                    if not continue_group:
                        break
                    updated_settings_host = settings_host + [f'compiler.cppstd={cppstd}']

                    profile_host, profile_build = compute_profiles(conan_api,
                                                                   profile_host=host_profile,
                                                                   settings_host=updated_settings_host,
                                                                   options_host=options_host,
                                                                   conf_host=conf_host,
                                                                   profile_build=build_profile,
                                                                   settings_build=settings_build,
                                                                   options_build=options_build,
                                                                   conf_build=conf_build,
                                                                   profile_folder=profile_folder)
                    try:
                        deps_graph = conan_api.graph.load_graph_requires([reference], tool_requires=[],
                                                                         profile_host=profile_host,
                                                                         profile_build=profile_build,
                                                                         lockfile=None, remotes=remotes,
                                                                         update=None, check_updates=False)
                        try:
                            deps_graph.report_graph_error()
                        except GraphConflictError as e:
                            conflicts.setdefault(str(reference), []).append({
                                "configuration": f"{host_profile} - {build_profile} - {cppstd}",
                                "conflict": str(e)
                            })
                        continue_group = False
                    except Exception as e:
                        import traceback
                        out.error(f"Error processing {reference}: {e}")
    return conflicts


def expand_profiles(conan_api, reference, profile_map):
    expanded_profiles = []
    export_path = conan_api.cache.export_path(reference)
    conanfile_path = conan_api.local.get_conanfile_path(export_path, os.getcwd(), py=True)
    # TODO: we need to keep a lockfile
    conanfile = conan_api.local.inspect(conanfile_path, remotes=None, lockfile=None)
    # It does not matter if they have package_type = [header-library|shared-library], it's only important
    # that the recipe contains the options shared or header_only
    has_header_option = conanfile.options.get_safe("header_only", None) is not None
    has_shared_option = conanfile.options.get_safe("shared", None) is not None

    for original_profile_info in profile_map['profiles']:
        profile_info = copy.deepcopy(original_profile_info)
        profile_info.setdefault('host_conf', [])
        profile_info.setdefault('build_conf', [])
        if has_shared_option:
            shared_profile_info = copy.deepcopy(profile_info)
            if has_header_option:
                shared_profile_info.setdefault('host_options', []).append("&:header_only=False")

            new_profile_shared = copy.deepcopy(shared_profile_info)
            new_profile_shared.setdefault('host_options', []).append("*:shared=True")
            expanded_profiles.append(new_profile_shared)

            new_profile_static = copy.deepcopy(shared_profile_info)
            new_profile_static.setdefault('host_options', []).append("*:shared=False")
            expanded_profiles.append(new_profile_static)
        if has_header_option:
            # Note: we're not explicitly passing a header_only=False configuration,
            # if this option exists for a recipe, it is assumed that `shared` is also an option
            # and that it is implicitly covered in the `has_shared` case
            new_profile_header = copy.deepcopy(profile_info)
            new_profile_header.setdefault('host_options', []).append("&:header_only=True")
            expanded_profiles.append(new_profile_header)
        if not has_shared_option and not has_header_option:
            expanded_profiles.append(profile_info)
    return expanded_profiles


def compute_profiles(conan_api,
                     profile_host, settings_host, options_host, conf_host,
                     profile_build, settings_build, options_build, conf_build,
                     profile_folder):
    profile_args = SimpleNamespace(
        profile_host=[os.path.join(profile_folder, profile_host)],
        profile_build=[os.path.join(profile_folder, profile_build)],
        settings_host=settings_host, options_host=options_host, conf_host=conf_host,
        settings_build=settings_build, options_build=options_build, conf_build=conf_build
    )

    profile_host, profile_build = conan_api.profiles.get_profiles_from_args(profile_args)
    return profile_host, profile_build



