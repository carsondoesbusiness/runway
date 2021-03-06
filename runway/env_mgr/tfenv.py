"""Terraform version management."""
import json
import logging
import os
import platform
import re
import shutil
import sys
import tempfile
import zipfile
from distutils.version import LooseVersion  # noqa pylint: disable=E

import hcl
import requests
# Old pylint on py2.7 incorrectly flags these
from six.moves.urllib.error import URLError  # pylint: disable=E
from six.moves.urllib.request import urlretrieve  # pylint: disable=E

from ..util import cached_property, get_hash_for_filename, sha256sum
from . import EnvManager, handle_bin_download_error

LOGGER = logging.getLogger(__name__)
TF_VERSION_FILENAME = '.terraform-version'


# Branch and local variable count will go down when py2 support is dropped
def download_tf_release(version,  # noqa pylint: disable=too-many-locals,too-many-branches
                        versions_dir, command_suffix, tf_platform=None,
                        arch=None):
    """Download Terraform archive and return path to it."""
    version_dir = versions_dir / version

    if arch is None:
        arch = (
            os.environ.get('TFENV_ARCH') if os.environ.get('TFENV_ARCH')
            else 'amd64')

    if tf_platform:
        tfver_os = tf_platform + '_' + arch
    else:
        if platform.system().startswith('Darwin'):
            tfver_os = "darwin_%s" % arch
        elif platform.system().startswith('Windows') or (
                platform.system().startswith('MINGW64') or (
                    platform.system().startswith('MSYS_NT') or (
                        platform.system().startswith('CYGWIN_NT')))):
            tfver_os = "windows_%s" % arch
        else:
            tfver_os = "linux_%s" % arch

    download_dir = tempfile.mkdtemp()
    filename = "terraform_%s_%s.zip" % (version, tfver_os)
    shasums_name = "terraform_%s_SHA256SUMS" % version
    tf_url = "https://releases.hashicorp.com/terraform/" + version

    try:
        LOGGER.verbose('downloading Terraform from %s...', tf_url)
        for i in [filename, shasums_name]:
            urlretrieve(tf_url + '/' + i,
                        os.path.join(download_dir, i))
    # IOError in py2; URLError in 3+
    except (IOError, URLError) as exc:
        handle_bin_download_error(exc, 'Terraform')

    tf_hash = get_hash_for_filename(filename, os.path.join(download_dir,
                                                           shasums_name))
    if tf_hash != sha256sum(os.path.join(download_dir, filename)):
        LOGGER.error("downloaded Terraform %s does not match sha256 %s",
                     filename, tf_hash)
        sys.exit(1)

    tf_zipfile = zipfile.ZipFile(os.path.join(download_dir, filename))
    version_dir.mkdir(parents=True, exist_ok=True)
    tf_zipfile.extractall(str(version_dir))
    tf_zipfile.close()
    shutil.rmtree(download_dir)
    result = version_dir / ('terraform' + command_suffix)
    result.chmod(result.stat().st_mode | 0o0111)  # ensure it is executable


def get_available_tf_versions(include_prerelease=False):
    """Return available Terraform versions."""
    tf_releases = json.loads(
        requests.get('https://releases.hashicorp.com/index.json').text
    )['terraform']
    tf_versions = sorted([k  # descending
                          for k, _v in tf_releases['versions'].items()],
                         key=LooseVersion,
                         reverse=True)
    if include_prerelease:
        return tf_versions
    return [i for i in tf_versions if '-' not in i]


def get_latest_tf_version(include_prerelease=False):
    """Return latest Terraform version."""
    return get_available_tf_versions(include_prerelease)[0]


class TFEnvManager(EnvManager):  # pylint: disable=too-few-public-methods
    """Terraform version management.

    Designed to be compatible with https://github.com/tfutils/tfenv.

    """

    def __init__(self, path=None):
        """Initialize class."""
        super(TFEnvManager, self).__init__('terraform', 'tfenv', path)

    @cached_property
    def backend(self):
        """Backend config of the Terraform module.

        Returns:
            Dict[str, Any]

        """
        # Terraform can only have one backend configured; this formats the
        # data to make it easier to work with
        return [
            {'type': k, 'config': v}
            for k, v in self.terraform_block.get('backend', {None: {}}).items()
        ][0]

    @cached_property
    def terraform_block(self):
        """Collect Terraform configuration blocks from a Terraform module.

        Returns:
            Dict[str, Any]

        """
        result = {}
        for tf_file in self.path.glob('*.tf'):
            tf_config = hcl.loads(tf_file.read_text())
            result.update(tf_config.get('terraform', {}))
        LOGGER.debug('parsed Terraform configuration: %s', json.dumps(result))
        return result

    @cached_property
    def version_file(self):
        """Find and return a ".terraform-version" file if one is present.

        Returns:
            Optional[Path]: Path to the Terraform version file.

        """
        for path in [self.path, self.path.parent]:
            test_path = path / TF_VERSION_FILENAME
            if test_path.is_file():
                LOGGER.debug('using version file: %s', test_path)
                return test_path
        return None

    def get_min_required(self):
        """Get the defined minimum required version of Terraform.

        Returns:
            str: The minimum required version as defined in the module.

        """
        version = self.terraform_block.get('required_version')

        if version:
            if re.match(r'^!=.+', version):
                LOGGER.error('min required Terraform version is a negation (%s) '
                             '- unable to determine required version',
                             version)
                sys.exit(1)
            else:
                version = re.search(
                    r'[0-9]*\.[0-9]*(?:\.[0-9]*)?', version
                ).group(0)
                LOGGER.debug("detected minimum Terraform version is %s",
                             version)
                return version
        LOGGER.error('Terraform version specified as min-required, but unable to '
                     'find a specified version requirement in this module\'s tf '
                     'files')
        sys.exit(1)

    def get_version_from_file(self, file_path=None):
        """Get Terraform version from a file.

        Args:
            file_path (Optional[Path]): Path to file that will be read.

        """
        file_path = file_path or self.version_file
        if file_path and file_path.is_file():
            return file_path.read_text().strip()
        LOGGER.debug(
            'file path not provided and version file could not be found'
        )
        return None

    def install(self, version_requested=None):
        """Ensure Terraform is available."""
        version_requested = version_requested or self.get_version_from_file()

        if not version_requested:
            raise ValueError(
                'version not provided and unable to find a {} file'.format(
                    TF_VERSION_FILENAME
                )
            )

        if re.match(r'^min-required$', version_requested):
            LOGGER.debug('tfenv: detecting minimal required version')
            version_requested = self.get_min_required()

        if re.match(r'^latest:.*$', version_requested):
            regex = re.search(r'latest:(.*)', version_requested).group(1)
            include_prerelease_versions = False
        elif re.match(r'^latest$', version_requested):
            regex = r'^[0-9]+\.[0-9]+\.[0-9]+$'
            include_prerelease_versions = False
        else:
            regex = "^%s$" % version_requested
            include_prerelease_versions = True
            # Return early (i.e before reaching out to the internet) if the
            # matching version is already installed
            if (self.versions_dir / version_requested).is_dir():
                LOGGER.verbose("Terraform version %s already installed; using "
                               "it...", version_requested)
                self.current_version = version_requested
                return str(self.bin)

        try:
            version = next(i
                           for i in get_available_tf_versions(
                               include_prerelease_versions)
                           if re.match(regex, i))
        except StopIteration:
            LOGGER.error("unable to find a Terraform version matching regex: %s",
                         regex)
            sys.exit(1)

        # Now that a version has been selected, skip downloading if it's
        # already been downloaded
        if (self.versions_dir / version).is_dir():
            LOGGER.verbose("Terraform version %s already installed; using it...",
                           version)
            self.current_version = version
            return str(self.bin)

        LOGGER.info("downloading and using Terraform version %s ...",
                    version)
        download_tf_release(version, self.versions_dir, self.command_suffix)
        LOGGER.verbose("downloaded Terraform %s successfully", version)
        self.current_version = version
        return str(self.bin)
