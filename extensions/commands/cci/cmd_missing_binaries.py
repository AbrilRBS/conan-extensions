import copy
import json
from types import SimpleNamespace
import os
import yaml
from conan.api.output import ConanOutput, cli_out_write
from conan.cli.command import conan_command
from conan.api.conan_api import ConanAPI
from conans.client.graph.install_graph import InstallGraph
from conans.errors import ConanException
from conans.model.recipe_ref import RecipeReference


def format_json(data):
    cli_out_write(json.dumps(data, indent=4))


@conan_command(group="Conan Center Index", formatters={"json": format_json, "text": format_json})
def missing_binaries(conan_api: ConanAPI, parser, *args):
    """Gets an ordered list of packages that need to be built once a new given reference is added to the graph

    This command runs the following steps:
    1. Ensures that the current cache is empty, so that no false positives are detected
    2. Exports the local conan center index clone to the cache using the `conan cci:export-all-versions` command in this repo
    3. For each exported package and profile passed as arguments, it checks if the package has the given reference as part of
       its required dependencies which affect its package id, but which does not match the given reference, so that it would
       need to be rebuilt.
    4. For every package that needs to be rebuilt, it generates a build order, and merged everything into a single list
    """
    parser.add_argument("--repo-path", help="Path to the local conan center index clone (contains the new version already)", required=True)
    parser.add_argument("--profiles-path", help="Path to the profiles folder", required=True)
    parser.add_argument("--profiles-map", help="Path to the profiles map file", required=True)
    parser.add_argument("--missing-packages", help="Path to the missing packages file", required=False)
    parser.add_argument("--generate-build-order", action="store_true", help="Generate a build order for the missing packages", default=False)

    args = parser.parse_args(*args)
    out = ConanOutput()

    with open(args.profiles_map) as json_file:
        profile_map = yaml.safe_load(json_file)

    out.info(f"Loaded {len(profile_map['profiles'])} base profiles")
    remotes = conan_api.remotes.list(["conancenter"])
    if not args.missing_packages:

        list_output = conan_api.command.run("list *")

        if len(list_output["results"]["Local Cache"]) != 0:
            raise ConanException("The cache must be empty to run this command, and the new reference must be present in the CCI clone")

        export_versions_output = conan_api.command.run(f"cci:export-all-versions -p {os.path.join(args.repo_path, 'recipes')}")

        exported_list = export_versions_output["exported_with_versions"]

        exported_list = [RecipeReference(*ref.split("/")) for ref in exported_list]

        out.info(f"Exported {len(exported_list)} references")

        out.title("Check which packages need to be rebuilt")

        missing_packages = generate_missing_packages(conan_api, exported_list, profile_map, args.profiles_path, remotes)
    else:
        with open(args.missing_packages) as f:
            missing_packages = json.load(f)

    if not missing_packages:
        out.info("No packages need to be rebuilt")
        return {}
    elif args.generate_build_order:
        out.info("Generating build order for missing packages")
        build_order = generate_build_order(conan_api, missing_packages, profile_map, args.profiles_path, remotes)
        with open("missing_packages_build_order_global.json", "w") as f:
            json.dump(build_order, f, indent=4)

    return missing_packages


def generate_missing_packages(conan_api, reference_list, profile_map, profile_folder, remotes):
    # Result variables
    missing_packages = {}
    out = ConanOutput()
    grouped_references = {}
    for reference in reference_list:
        grouped_references.setdefault(reference.name, []).append(reference)

    total_groups = len(grouped_references)
    for i, (group, references) in enumerate(grouped_references.items()):
        out.info(f"Checking group {i + 1}/{total_groups} of references ({group})")
        for reference in references:
            profiles = expand_profiles(conan_api, reference, profile_map)
            continue_reference = True
            for profile_info in profiles:
                if not continue_reference:
                    break
                host_profile = profile_info['host_profile']
                build_profile = profile_info['build_profile']
                cppstd_values = profile_info['cppstd']

                options_host = profile_info.get('host_options', [])
                options_build = profile_info.get('build_options', [])
                settings_host = profile_info.get('host_settings', [])
                settings_build = profile_info.get('build_settings', [])
                conf_host = profile_info.get('host_conf', [])
                conf_build = profile_info.get('build_conf', [])
                for cppstd in cppstd_values:
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
                        out.info(f"Checking {reference} with profile {host_profile} {build_profile} c++{cppstd}")
                        # There's a possible optimization here: Expose DepsGraphBuilder internal loader cache to reuse for every reference
                        deps_graph = conan_api.graph.load_graph_requires([reference], tool_requires=[],
                                                                         profile_host=profile_host,
                                                                         profile_build=profile_build,
                                                                         lockfile=None, remotes=remotes,
                                                                         update=None, check_updates=False)
                        deps_graph.report_graph_error()
                        # There's a possible optimization here: Directly use a unique GraphBinariesAnalyzer instance
                        # to keep its cache around between references
                        conan_api.graph.analyze_binaries(deps_graph,
                                                         build_mode=None,
                                                         remotes=remotes,
                                                         update=None,
                                                         lockfile=None)
                        if deps_graph.nodes[1].binary == "Invalid":
                            continue
                        if deps_graph.nodes[1].binary == "Missing":
                            missing_packages.setdefault(str(reference), []).append([host_profile, build_profile, cppstd])
                            # This depsgraph can't be used to generate a InstallGraph, we'll need to do it after
                            out.warning(f"Found missing package for {reference} with profile {host_profile} {build_profile} c++{cppstd}")
                            continue_reference = False
                        out.success(f"Valid calculation for {reference}")
                        # No need to check further cppstds
                        break
                    except Exception as e:
                        import traceback
                        out.error(f"Error processing {reference}: {e}")
    return missing_packages


def generate_build_order(conan_api, missing_packages, profile_map, profile_folder, remotes):
    build_mode = [f"missing:{package}" for package in missing_packages]
    merged_install_graph = None
    out = ConanOutput()
    grouped_references = {}
    reference_list = [RecipeReference(*ref.split("/")) for ref in missing_packages.keys()]
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
                for cppstd in cppstd_values:
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
                        out.info(f"Checking {reference} with profile {host_profile} {build_profile} c++{cppstd}")
                        # There's a possible optimization here: Expose DepsGraphBuilder internal loader cache to reuse for every reference
                        deps_graph = conan_api.graph.load_graph_requires([reference], tool_requires=[],
                                                                         profile_host=profile_host,
                                                                         profile_build=profile_build,
                                                                         lockfile=None, remotes=remotes,
                                                                         update=None, check_updates=False)
                        deps_graph.report_graph_error()
                        # There's a possible optimization here: Directly use a unique GraphBinariesAnalyzer instance
                        # to keep its cache around between references
                        conan_api.graph.analyze_binaries(deps_graph,
                                                         build_mode=build_mode,
                                                         remotes=remotes,
                                                         update=None,
                                                         lockfile=None)
                        if deps_graph.nodes[1].binary == "Invalid":
                            continue
                        new_install_graph = InstallGraph(deps_graph, order_by="recipe")
                        if merged_install_graph is None:
                            merged_install_graph = new_install_graph
                        else:
                            merged_install_graph.merge(new_install_graph)

                        out.warning(f"Found missing package for {reference} with profile {host_profile} {build_profile} c++{cppstd}")
                        # No need to check further cppstds
                        break
                    except Exception as e:
                        import traceback
                        out.error(f"Error processing {reference}: {e}")
    merged_install_graph.reduce()
    return merged_install_graph.install_build_order()


def version_repr_matches(version_repr, version):
    # 1.2.Z -> 1.2.3
    # (1, 1), (2, 2), (Z, 3)
    for reprt_item, version_item in zip(version_repr.split('.'), str(version).split('.')):
        if not str(reprt_item).isdigit():
            return True
        if reprt_item != version_item:
            return False
    return True


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



