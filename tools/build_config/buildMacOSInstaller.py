#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Eclipse SUMO, Simulation of Urban MObility; see https://eclipse.dev/sumo
# Copyright (C) 2008-2025 German Aerospace Center (DLR) and others.
# This program and the accompanying materials are made available under the
# terms of the Eclipse Public License 2.0 which is available at
# https://www.eclipse.org/legal/epl-2.0/
# This Source Code may also be made available under the following Secondary
# Licenses when the conditions for such availability set forth in the Eclipse
# Public License 2.0 are satisfied: GNU General Public License, version 2
# or later which is available at
# https://www.gnu.org/licenses/old-licenses/gpl-2.0-standalone.html
# SPDX-License-Identifier: EPL-2.0 OR GPL-2.0-or-later

# @file    buildMacOSInstaller.py
# @author  Robert Hilbrich
# @date    2024-07-16

# Creates the macOS installer for the current version of SUMO.
# It requires a build to have completed successfully.
# This build directory needs to be provided as parameter.
#
# This script will then:
# 1. Create a pkg file for Eclipse SUMO (= "framework") (and also add dependent libraries)
# 2. Create pkg files for all launchers (= "apps")
# 3. Create an installer pkg to jointly install the framework pkg and all app pkgs
# 4. Put the installer pkg into a dmg - ready to be notarized

import os
import plistlib
import re
import shutil
import string
import subprocess
import sys
import tempfile
from glob import iglob

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sumolib.options import ArgumentParser  # noqa

from build_config.version import get_pep440_version  # noqa

try:
    from delocate.cmd.delocate_path import delocate_path
except ImportError:
    print("Error: delocate module is not installed. Please install it using 'pip install delocate'.")
    sys.exit(1)

try:
    from dmgbuild.core import build_dmg
except ImportError:
    print("Error: dmgbuild module is not installed. Please install it using 'pip install dmgbuild'.")
    sys.exit(1)


def transform_pep440_version(version):
    post_pattern = re.compile(r"^(.*)\.post\d+$")
    match = post_pattern.match(version)
    if match:
        return f"{match.group(1)}-git"
    else:
        return version


def parse_args(def_dmg_name, def_pkg_name):
    def_output_dmg_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", def_dmg_name))
    def_output_pkg_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", def_pkg_name))

    op = ArgumentParser(description="Build an installer for macOS (dmg file)")
    op.add_argument("build_dir", help="Build dir of sumo")
    op.add_argument("--output-pkg", dest="output_pkg", help="Output path for pkg", default=def_output_pkg_path)
    op.add_argument("--output-dmg", dest="output_dmg", help="Output path for dmg", default=def_output_dmg_path)
    op.add_argument("--output-framework-dir", dest="output_framework_dir")

    op.add_option("-v", "--verbose", action="store_true", default=False, help="tell me more")

    return op.parse_args()


def create_installer_conclusion_content(framework_name):
    template_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <style>
        body {
            font-family: Helvetica;
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div>
        <h4>Important:</h4>
        <ul>
            <li>
                For applications with a graphical user interface to function properly, please ensure
                you have <b>XQuartz</b> installed.
                It can be obtained from: <a href="https://www.xquartz.org" target="_blank">XQuartz</a>.
            </li>
            <li>
                If you intend to use SUMO from the command line, please remember to set
                the <b>SUMO_HOME</b> environment variable
                <br>
                For more details, visit the
                <a href="https://sumo.dlr.de/docs/Installing/index.html#macos" target="_blank">
                    SUMO macOS installation guide
                </a>.
            </li>
        </ul>
        </p>
        <p>For support options, including the "sumo-user" mailing list, please visit:
           <a href="https://eclipse.dev/sumo/contact/" target="_blank">SUMO Contact</a>.
        </p>
    </div>
</body>
</html>
"""
    template = string.Template(template_html)
    context = {
        "framework_name": framework_name,
    }
    html = template.substitute(context)
    return html


def sign_file(file_path):
    """Signs a file using the specified signing service."""
    signing_service_url = "https://cbi-staging.eclipse.org/macos/codesign/sign"
    signed_file_path = f"{file_path}-signed"

    # Create a temporary entitlements file
    with tempfile.NamedTemporaryFile(delete=False, suffix=".entitlement") as temp_entitlements:
        temp_entitlements.write(b'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.cs.allow-jit</key>
    <true/>
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
    <true/>
    <key>com.apple.security.cs.disable-executable-page-protection</key>
    <true/>
    <key>com.apple.security.cs.allow-dyld-environment-variables</key>
    <true/>
    <key>com.apple.security.cs.disable-library-validation</key>
    <true/>
    <key>com.apple.security.cs.debugger</key>
    <true/>
</dict>
</plist>
''')

    try:
        command = [
            "curl",
            "-o", signed_file_path,
            "-F", f"file=@{file_path}",
            "-F", f"entitlements=@{temp_entitlements.name}",
            signing_service_url,
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to sign file {file_path}: {result.stderr}")
        if not os.path.exists(signed_file_path):
            raise FileNotFoundError(f"Signed file not created: {signed_file_path}")
        return signed_file_path
    finally:
        os.remove(temp_entitlements.name)


def create_framework(name, longname, pkg_id, version, sumo_build_directory, output_dir=None):
    print(" - Creating directory structure")

    # Use the provided framework_dir or create a temporary directory
    if output_dir:
        if not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)
        temp_dir = output_dir
    else:
        temp_dir = tempfile.mkdtemp()

    # Create the directory structure for the framework bundle
    # see: https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPFrameworks/Concepts/FrameworkAnatomy.html  # noqa
    #
    # EclipseSUMO.framework/
    # ├── EclipseSUMO   --> Versions/Current/EclipseSUMO
    # ├── Resources     --> Versions/Current/Resources
    # └── Versions
    #     ├── v1_20_0
    #     │   ├── EclipseSUMO
    #     │   └── Resources
    #     │       └── Info.plist
    #     └── Current   --> v_1_20_0

    framework_dir = os.path.join(temp_dir, f"{name}.framework")
    version_dir = os.path.join(framework_dir, f"Versions/{version}")
    os.makedirs(os.path.join(version_dir, name), exist_ok=True)
    os.makedirs(os.path.join(version_dir, "Resources"), exist_ok=True)

    os.symlink(f"{version}/", os.path.join(framework_dir, "Versions/Current"), True)
    os.symlink(f"Versions/Current/{name}/", os.path.join(framework_dir, name), True)
    os.symlink("Versions/Current/Resources/", os.path.join(framework_dir, "Resources"), True)

    # Create the Info.plist file
    plist_file = os.path.join(version_dir, "Resources", "Info.plist")
    print(" - Creating plist file")
    plist_content = {
        "CFBundleExecutable": longname,
        "CFBundleIdentifier": pkg_id,
        "CFBundleName": longname,
        "CFBundleVersion": version,
        "CFBundleShortVersionString": version,
    }
    with open(plist_file, "wb") as f:
        plistlib.dump(plist_content, f)

    # Copy files from the current repository clone to version_dir/EclipseSUMO
    print(" - Calling cmake install")
    cmake_install_command = [
        "cmake",
        "--install",
        sumo_build_directory,
        "--prefix",
        os.path.join(version_dir, name),
    ]
    subprocess.run(cmake_install_command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # We need to add a symlink to the binary folder to have the same folder structure
    os.symlink("../../bin", os.path.join(version_dir, name, "share", "sumo", "bin"))

    # Determine library dependencies
    print(" - Delocating binaries and copying all libraries  - this may take a while")
    os.chdir(os.path.join(version_dir, name))

    # - libraries that landed in the lib folder need to be delocated as well
    lib_dir = os.path.join(version_dir, name, "lib")
    bin_dir = os.path.join(version_dir, name, "bin")

    for pattern in ("*.jnilib", "*.dylib"):
        for file in iglob(os.path.join(lib_dir, pattern)):
            file_name = os.path.basename(file)
            shutil.move(os.path.join(lib_dir, file_name), os.path.join(bin_dir, file_name))

    # Start the delocation of the libraries and binaries
    delocate_path("./bin", lib_filt_func=None, lib_path="./lib", sanitize_rpaths=True)

    # - and we need to move them back to the lib folder
    for pattern in ("*.jnilib", "*.dylib"):
        for file in iglob(os.path.join(bin_dir, pattern)):
            file_name = os.path.basename(file)
            shutil.move(os.path.join(bin_dir, file_name), os.path.join(lib_dir, file_name))

    # Add proj db files from /opt/homebrew/Cellar/proj/<X.Y.Z>/share/proj
    print(" - Copying proj.db files")
    proj_dir = "/opt/homebrew/Cellar/proj"
    proj_file_list = ["GL27", "ITRF2000", "ITRF2008", "ITRF2014", "nad.lst", "nad27", "nad83", "other.extra", "proj.db",
                      "proj.ini", "projjson.schema.json", "triangulation.schema.json", "world", "CH", "deformation_model.schema.json"]  # noqa
    dest_dir = os.path.join(version_dir, name, "share", "proj")
    if os.path.exists(proj_dir):
        first_dir = next(iter(os.listdir(proj_dir)), None)
        if first_dir:
            source_dir = os.path.join(proj_dir, first_dir, "share/proj")
            if os.path.exists(source_dir):
                os.makedirs(dest_dir, exist_ok=True)
                for file in proj_file_list:
                    shutil.copy2(os.path.join(source_dir, file), os.path.join(dest_dir, file))

    # # Signing the binaries and libraries
    # print(" - Signing binaries and libraries")
    # for dir_path in [bin_dir, lib_dir]:
    #     for file in os.listdir(dir_path):
    #         file_path = os.path.join(dir_path, file)
    #         if os.path.isfile(file_path):
    #             print(f"    . Signing {file_path}...")
    #             signed_file_path = sign_file(file_path)
    #             shutil.move(signed_file_path, file_path)

    # Build the framework package
    cwd = os.path.dirname(os.path.abspath(__file__))
    pkg_name = f"{name}-{version}.pkg"
    pkg_path = os.path.join(cwd, "..", "..", pkg_name)
    pkg_build_command = [
        "pkgbuild",
        "--root",
        framework_dir,
        "--identifier",
        pkg_id,
        "--version",
        version,
        "--install-location",
        f"/Library/Frameworks/{name}.framework",
        f"{pkg_path}",
    ]
    # print(" - Calling pkgbuild")
    # subprocess.run(pkg_build_command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # pkg_size = os.path.getsize(pkg_path)

    # # Cleanup the temporary directory
    # print(" - Cleaning up")
    # shutil.rmtree(temp_dir)
    # return name, pkg_name, pkg_id, pkg_path, pkg_size
    return name, pkg_name, pkg_id, pkg_path, 0


def create_app(app_name, exec_call, framework_name, pkg_id, version, icns_path, verbose):
    print(" - Creating directory structure")
    temp_dir = tempfile.mkdtemp()

    # Example app structure:
    # SUMO-GUI.app
    # └── Contents
    #     └── Info.plist
    #     ├── MacOS
    #     │   └── SUMO-GUI
    #     └── Resources
    #         └── iconsfile.icns

    os.makedirs(os.path.join(temp_dir, f"{app_name}.app", "Contents", "MacOS"))
    os.makedirs(os.path.join(temp_dir, f"{app_name}.app", "Contents", "Resources"))

    print(" - Creating launcher")
    launcher_content = f"""#!/bin/bash
export SUMO_HOME="/Library/Frameworks/{framework_name}.framework/Versions/Current/{framework_name}/share/sumo"
{exec_call}
"""
    launcher_path = os.path.join(temp_dir, f"{app_name}.app", "Contents", "MacOS", app_name)
    with open(launcher_path, "w") as launcher:
        launcher.write(launcher_content)
    os.chmod(launcher_path, 0o755)

    # Copy the icons
    print(" - Copying icons")
    shutil.copy(icns_path, os.path.join(temp_dir, f"{app_name}.app", "Contents", "Resources", "iconfile.icns"))

    # Create plist file
    print(" - Creating plist file")
    plist_file = os.path.join(temp_dir, f"{app_name}.app", "Contents", "Info.plist")
    plist_content = {
        "CFBundleExecutable": app_name,
        "CFBundleIdentifier": pkg_id,
        "CFBundleName": app_name,
        "CFBundleVersion": version,
        "CFBundleShortVersionString": version,
        "CFBundleIconFile": "iconfile.icns",
    }
    with open(plist_file, "wb") as f:
        plistlib.dump(plist_content, f)

    # Call pkg builder
    print(" - Calling pkgbuild")
    cwd = os.path.dirname(os.path.abspath(__file__))
    pkg_name = f"Launcher-{app_name}-{version}.pkg"
    pkg_path = os.path.join(cwd, "..", "..", pkg_name)
    pkg_build_command = [
        "pkgbuild",
        "--root",
        os.path.join(temp_dir, f"{app_name}.app"),
        "--identifier",
        pkg_id,
        "--version",
        version,
        "--install-location",
        f"/Applications/{app_name}.app",
        f"{pkg_path}",
    ]
    if verbose:
        print(pkg_build_command)
    sys.stdout.flush()
    out = None if verbose else subprocess.DEVNULL
    subprocess.run(pkg_build_command, check=True, stdout=out, stderr=out)
    pkg_size = os.path.getsize(pkg_path)

    # Cleanup the temporary directory
    print(" - Cleaning up")
    shutil.rmtree(temp_dir)
    return app_name, pkg_name, pkg_id, pkg_path, pkg_size


def create_installer(frmwk_pkg, app_pkgs, id, version, output_path, verbose):
    cwd = os.path.dirname(os.path.abspath(__file__))
    print(" - Creating temporary directory")
    temp_dir = tempfile.mkdtemp()
    resources_dir = os.path.join(temp_dir, "Resources")
    os.makedirs(resources_dir)

    # Copy the framework package
    framework_pkg_path = frmwk_pkg[3]
    shutil.copy(framework_pkg_path, temp_dir)

    # Copy the app packages
    for app_pkg in app_pkgs:
        app_pkg_path = app_pkg[3]
        shutil.copy(app_pkg_path, temp_dir)

    # Add license, background and other nice stuff
    print(" - Adding additional resources to the installer")
    sumo_dir = os.path.join(cwd, "..", "..", "")
    sumo_data_installer_dir = os.path.join(sumo_dir, "build_config", "macos", "installer")
    installer_resources_dir = os.path.join(temp_dir, "Resources")
    shutil.copy(os.path.join(sumo_data_installer_dir, "background.png"), installer_resources_dir)
    shutil.copy(os.path.join(sumo_dir, "LICENSE"), os.path.join(installer_resources_dir, "LICENSE.txt"))

    # Create conclusion.html in the installer resources folder
    with open(os.path.join(installer_resources_dir, "conclusion.html"), "w") as file:
        file.write(create_installer_conclusion_content(frmwk_pkg[0]))

    # Create distribution.xml
    print(" - Creating distribution.xml")

    size = frmwk_pkg[4] // 1024
    path = os.path.basename(frmwk_pkg[3])
    refs = f"        <pkg-ref id='{frmwk_pkg[2]}' version='{version}' installKBytes='{size}'>{path}</pkg-ref>"

    for _, app_pkg in enumerate(app_pkgs):
        size = app_pkg[4] // 1024
        path = os.path.basename(app_pkg[3])
        refs += f"\n        <pkg-ref id='{app_pkg[2]}' version='{version}' installKBytes='{size}'>{path}</pkg-ref>"

    # See: https://developer.apple.com/library/archive/documentation/
    #      DeveloperTools/Reference/DistributionDefinitionRef/Chapters/Distribution_XML_Ref.html
    distribution_content = f"""<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
    <title>Eclipse SUMO</title>
    <allowed-os-versions><os-version min="10.14"/></allowed-os-versions>
    <license file="LICENSE.txt"/>
    <background file="background.png" alignment="bottomleft" mime-type="image/png" scaling="none" />
    <conclusion file="conclusion.html" mime-type="text/html"/>
    <options customize="allow" require-scripts="false" rootVolumeOnly="true" hostArchitectures="arm64"/>
    <choices-outline>
        <line choice="default"/>
    </choices-outline>
    <choice id="default" title="Eclipse SUMO {version}">
{refs}
    </choice>
</installer-gui-script>
"""
    distribution_path = os.path.join(temp_dir, "distribution.xml")
    with open(distribution_path, "w") as f:
        f.write(distribution_content)

    # Call productbuild
    print(" - Calling productbuild")
    productbuild_command = [
        "productbuild",
        "--distribution",
        distribution_path,
        "--package-path",
        temp_dir,
        "--resources",
        resources_dir,
        output_path,
    ]
    sys.stdout.flush()
    out = None if verbose else subprocess.DEVNULL
    subprocess.run(productbuild_command, check=True, stdout=out, stderr=out)
    pkg_size = os.path.getsize(output_path)

    print(" - Cleaning up")
    shutil.rmtree(temp_dir)

    return "Installer", os.path.basename(output_path), id, output_path, pkg_size


def create_dmg(dmg_title, dmg_output_path, installer_pkg_path):

    print(" - Preparing disk image folder")
    dmg_prep_folder = tempfile.mkdtemp()
    shutil.copy(installer_pkg_path, dmg_prep_folder)

    # FIXME: Add uninstaller script to the installer folder

    if os.path.exists(dmg_output_path):
        print(" - Removing already existing disk image before creating a new disk image")
        os.remove(dmg_output_path)

    print(" - Collecting files and calculating file size")
    files_to_store = []
    total_size = 0
    for root, _, files in os.walk(dmg_prep_folder):
        for file in files:
            files_to_store.append((os.path.join(root, file), file))
            total_size += os.path.getsize(os.path.join(root, file))

    print(" - Building diskimage")
    settings = {
        "volume_name": "Eclipse SUMO",
        "size": f"{total_size // 1024 * 1.2}K",
        "files": files_to_store,
        # FIXME: add background and badge
    }
    build_dmg(dmg_output_path, dmg_title, settings=settings)

    print(" - Cleaning up")
    shutil.rmtree(dmg_prep_folder)


def main():
    version = transform_pep440_version(get_pep440_version())
    default_pkg_name = f"sumo-{version}.pkg"
    default_dmg_name = f"sumo-{version}.dmg"
    base_id = "org.eclipse.sumo"

    # Parse and check the command line arguments
    opts = parse_args(default_dmg_name, default_pkg_name)
    if not os.path.exists(opts.build_dir):
        print(f"Error: build directory '{opts.build_dir}' does not exist.", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(os.path.join(opts.build_dir, "CMakeCache.txt")):
        print(f"Error: directory '{opts.build_dir}' is not a build directory.", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(os.path.dirname(opts.output_dmg)):
        print(f"Error: dmg output directory '{os.path.dirname(opts.output_dmg)}' does not exist.", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(os.path.dirname(opts.output_pkg)):
        print(f"Error: pkg output directory '{os.path.dirname(opts.output_pkg)}' does not exist.", file=sys.stderr)
        sys.exit(1)

    # Building the framework package
    print("Building framework package 'EclipseSUMO'")
    framework_pkg = create_framework("EclipseSUMO", "Eclipse SUMO", f"{
                                     base_id}.framework", version, opts.build_dir, opts.output_framework_dir)
    print(f"Successfully built: '{framework_pkg[1]}' ({framework_pkg[4] / (1024 * 1024):.2f} MB)\n")

    # # Building all the app launchers packages
    # cwd = os.path.dirname(os.path.abspath(__file__))
    # app_pkgs = []
    # app_list = [
    #     (
    #         "SUMO sumo-gui",
    #         'exec "$SUMO_HOME/bin/sumo-gui" "$@" &',
    #         framework_pkg[0],
    #         f"{base_id}.apps.sumo-gui",
    #         version,
    #         "sumo-gui.icns",
    #     ),
    #     (
    #         "SUMO netedit",
    #         'exec "$SUMO_HOME/bin/netedit" "$@" &',
    #         framework_pkg[0],
    #         f"{base_id}.apps.netedit",
    #         version,
    #         "netedit.icns",
    #     ),
    #     (
    #         "SUMO Scenario Wizard",
    #         (
    #             "python  $SUMO_HOME/share/sumo/tools/osmWebWizard.py ||"
    #             "python3 $SUMO_HOME/share/sumo/tools/osmWebWizard.py  &"
    #         ),
    #         framework_pkg[0],
    #         f"{base_id}.apps.scenario-wizard",
    #         version,
    #         "scenario-wizard.icns",
    #     ),
    # ]
    # for app_name, app_binary, app_framework, app_id, app_ver, app_icons in app_list:
    #     print(f"Building app package for '{app_name}'")

    #     icon_path = os.path.join(cwd, "..", "..", "build_config", "macos", "installer", app_icons)
    #     app_pkg = create_app(app_name, app_binary, app_framework, app_id, app_ver, icon_path, opts.verbose)
    #     app_pkgs.append(app_pkg)
    #     print(f"Successfully built: '{app_pkg[1]}' ({app_pkg[4] / (1024 * 1024):.2f} MB)\n")

    # # Building the installer package
    # print("Building installer")
    # installer_pkg = create_installer(framework_pkg, app_pkgs, f"{base_id}.installer", version,
    #                                  opts.output_pkg, opts.verbose)
    # print("Successfully built installer\n")

    # # Putting the installer package into a dmg file - ready for signing
    # print("Building disk image")
    # create_dmg("Eclipse SUMO", opts.output_dmg, installer_pkg[3])
    # print("Successfully built disk image\n")

    # # Removing non-installer pkg-files
    # os.remove(framework_pkg[3])
    # for app_pkg in app_pkgs:
    #     os.remove(app_pkg[3])

    # print("Build completed successfully")
    # print(f" - disk image   : '{opts.output_dmg}' ({os.path.getsize(opts.output_dmg) / (1024 * 1024):.2f} MB)")
    # print(f" - installer pkg: '{opts.output_pkg}' ({os.path.getsize(opts.output_pkg) / (1024 * 1024):.2f} MB)")


if __name__ == "__main__":
    main()
