import copy
import json
from types import SimpleNamespace
import os
import yaml
from conan.api.output import ConanOutput
from conan.cli.command import conan_command
from conan.api.conan_api import ConanAPI
from conans.client.graph.install_graph import InstallGraph
from conans.errors import ConanException
from conans.model.recipe_ref import RecipeReference


@conan_command(group="Conan Center Index")
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
    parser.add_argument("reference", help="Reference that will be added to the graph, in the form of name/version")
    parser.add_argument("--repo-path", help="Path to the local conan center index clone (contains the new version already)", required=True)
    parser.add_argument("--profiles-path", help="Path to the profiles folder", required=True)
    parser.add_argument("--profiles-map", help="Path to the profiles map file", required=True)
    # Used so if we have precomputed the packages to build, we can pass them in
    parser.add_argument("--packages-to-build", help="Path to the packages to build precomputed file if already calculated")

    args = parser.parse_args(*args)
    out = ConanOutput()

    with open(args.profiles_map) as json_file:
        profile_map = yaml.safe_load(json_file)

    out.info(f"Loaded {len(profile_map['profiles'])} base profiles")

    list_output = conan_api.command.run("list *")
    if not args.packages_to_build:
        if len(list_output["results"]["Local Cache"]) != 0:
            raise ConanException("The cache must be empty to run this command, and the new reference must be present in the CCI clone")

        export_versions_output = conan_api.command.run(f"cci:export-all-versions -p {os.path.join(args.repo_path, 'recipes')}")
    else:
        with open(args.packages_to_build) as f:
            precomputed_packages_to_build = json.load(f)
        export_versions_output = {
            "exported_with_versions": [args.reference,
                                       *[ref for ref in list_output["results"]["Local Cache"] if ref.split("/")[0] in precomputed_packages_to_build]]
        }

    exported_list = export_versions_output["exported_with_versions"]
    if args.reference not in exported_list:
        raise ConanException(f"The new reference {args.reference} was not found in the exported list. Please ensure it is present in the CCI clone")
    exported_list = [RecipeReference(*ref.split("/")) for ref in exported_list]

    out.info(f"Exported {len(exported_list)} references")

    out.title("Check which packages need to be rebuilt")

    remotes = conan_api.remotes.list(["conancenter"])
    new_reference = RecipeReference(*args.reference.split("/"))
    build_args = [f"missing:{ref.name}" for ref in exported_list]

    packages_to_build, install_graphs = generate_build_packages(conan_api, new_reference, exported_list, build_args, profile_map, args.profiles_path, remotes)

    if not packages_to_build:
        out.info("No packages need to be rebuilt")
        return

    out.info(f"Generated {len(packages_to_build)} packages to build")

    if not install_graphs:
        raise ConanException("No install orders were generated")

    with open("packages_to_build.json", "w") as f:
        json.dump(packages_to_build, f)

    merged_install_graphs = install_graphs[0]
    if len(install_graphs) > 1:
        for install_graph in install_graphs[1:]:
            merged_install_graphs.merge(install_graph)
        # merged_install_graphs.reduce()

    out.info(f"Merged {len(install_graphs)} install orders")

    install_build_order = merged_install_graphs.install_build_order()

    # Remove anything not in the packages to build, those are the only ones we care about,
    # The rest are here as a byproduct of the install order generation, as we don't check for
    # actual missing binaries
    for_tapaholes = []
    for batch in install_build_order["order"]:
        new_level = []
        for item in batch:
            if item["ref"].split("/")[0] in packages_to_build:
                new_level.append(item["ref"].split("#")[0])
        if new_level:
            for_tapaholes.append(new_level)

    out.info(f"Generated {len(for_tapaholes)} levels of build orders")
    out.info(json.dumps(for_tapaholes, indent=4))

    with open("super_duper_build_order.json", "w") as f:
        json.dump(install_build_order, f)

    with open("final_order.json", "w") as f:
        json.dump(for_tapaholes, f)


def generate_build_packages(conan_api, new_reference, reference_list, build_args, profile_map, profile_folder, remotes):
    # Result variables
    packages_to_build = []
    install_orders = []
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
                        deps_graph.report_graph_error()
                        conan_api.graph.analyze_binaries(deps_graph,
                                                         build_mode=build_args,
                                                         remotes=[],
                                                         update=None,
                                                         lockfile=None)
                        if deps_graph.nodes[1].binary == "Invalid":
                            continue

                        # If openssl is part of my dependencies that affect my pkgid, then I need to be rebuilt
                        requires = deps_graph.nodes[1].conanfile.info.requires.serialize()
                        for req in requires:
                            req_name, req_pattern = req.split("/", 1)
                            if req_name == new_reference.name and version_repr_matches(req_pattern, new_reference.version):
                                # Remains to be seen if this is a version range or pinned requirement
                                packages_to_build.append(reference.name)

                                # Calculate the install order
                                install_graph = InstallGraph(deps_graph, order_by="recipe")
                                install_orders.append(install_graph)

                                # If one in the group has openssl, assume we will need to build the rest of the references

                                break
                        continue_group = False
                    except Exception as e:
                        import traceback
                        out.error(f"Error processing {reference}: {e}")
    return packages_to_build, install_orders


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



