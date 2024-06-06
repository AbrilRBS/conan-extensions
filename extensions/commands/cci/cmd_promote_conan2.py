from conan.cli.command import conan_command
from conan.test.utils.test_files import temp_folder
import json
import os


@conan_command(group="Conan Center Index")
def promote_conan2(conan_api, parser, *args):
    """-"""
    parser.add_argument('path', help="Path to global recipes folder")
    parser.add_argument("-t", "--temp-folder", action="store", help="Temporary folder to store the promotion pkglist")
    args = parser.parse_args(*args)

    tmp = args.temp_folder or temp_folder()

    if not os.path.exists(tmp):
        os.makedirs(tmp)

    recipes = sorted(os.listdir(args.path))
    for lib in recipes:
        print(lib)
        output = conan_api.command.run(f'list {lib}/*#*:*#* -r conancenter')
        print(output)
        pkglist_promotion_path = os.path.join(tmp, f"{lib}.json")
        with open(pkglist_promotion_path, "w") as f:
            json.dump(output["results"], f, indent=4)

        # TODO: Missing args
        output = conan_api.command.run(f'art:promote {pkglist_promotion_path}')
        print(output)