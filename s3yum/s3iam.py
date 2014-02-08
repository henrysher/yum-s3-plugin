#!/usr/bin/env python
# Copyright 2012, Julius Seporaitis
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


__author__ = "Julius Seporaitis"
__email__ = "julius@seporaitis.net"
__copyright__ = "Copyright 2012, Julius Seporaitis"
__license__ = "Apache 2.0"
__version__ = "1.0.1"


import os
import sys
import urllib2
import urlparse
import time
import hashlib
import hmac
import base64
import json

import yum
import yum.config
import yum.Errors
import yum.plugins

from yum.yumRepo import YumRepository

CONFIG_PATH = "/etc/s3yum.cfg"

__all__ = ['requires_api_version', 'plugin_type', 'CONDUIT',
           'config_hook', 'init_hook']

requires_api_version = '2.5'
plugin_type = yum.plugins.TYPE_CORE
CONDUIT = None


def _check_s3_urls(urls):
    pattern = "s3.*\.amazonaws\.com"
    if isinstance(urls, basestring):
        if re.compile(pattern).findall(urls) != []:
            return True
    elif isinstance(urls, list):
        for url in urls:
            if re.compile(pattern).findall(url) == []:
                break
        else:
            return True
    return False


def init_hook(conduit):
    """Setup the S3 repositories."""

    repos = conduit.getRepos()
    for key, repo in repos.repos.iteritems():
        if isinstance(repo, YumRepository) and repo.enabled:
            if not _check_s3_urls(repo.baseurl):
                continue
            new_repo = S3Repository(repo.id, repo.baseurl)
            new_repo.name = repo.name
            new_repo.basecachedir = repo.basecachedir
            new_repo.base_persistdir = repo.base_persistdir
            new_repo.gpgcheck = repo.gpgcheck
            new_repo.proxy = repo.proxy
            new_repo.enablegroups = repo.enablegroups
            repos.delete(key)
            repos.add(new_repo)


class S3Repository(YumRepository):
    """Repository object for Amazon S3, using IAM Roles."""

    def __init__(self, repoid, baseurl):
        super(S3Repository, self).__init__(repoid)
        self.iamrole = None
        self.token = None
        self.baseurl = baseurl
        self.grabber = None

        # Inherited from YumRepository <-- Repository
        self.enable()

    @property
    def grabfunc(self):
        raise NotImplementedException("grabfunc called, when it shouldn't be!")

    @property
    def grab(self):
        if not self.grabber:
            self.grabber = S3Grabber(self)
            if not self.grabber.get_role():
                if not self.grabber.get_credentials_from_config():
                    raise yum.plugins.PluginYumExit("Failed to get credentials"
                                                    " from config file:"
                                                    " %s" % CONFIG_PATH)
            else:
                self.grabber.get_credentials_from_iamrole()
        return self.grabber


class S3Grabber(object):

    def __init__(self, repo):
        """Initialize file grabber.
           FIXME: cannot show the correct baseurl in use.
        """
        if isinstance(repo.baseurl, basestring):
            self.baseurl = [baseurl]
        else:
            if len(repo.baseurl) == 0:
                raise yum.plugins.PluginYumExit("s3iam: repository '{0}' "
                                                "must have only one "
                                                "'baseurl' value" % repo.id)
            else:
                self.baseurl = repo.baseurl

    def get_role(self):
        """Read IAM role from AWS metadata store."""
        request = urllib2.Request(
            urlparse.urljoin(
                "http://169.254.169.254",
                "/latest/meta-data/iam/security-credentials/"
            ))

        response = None
        try:
            response = urllib2.urlopen(request)
            self.iamrole = (response.read())
        except Exception as msg:
            if "HTTP Error 404" in msg:
                return False
        finally:
            if response:
                response.close()
                return True

    def get_credentials_from_config(self):
        """Read S3 credentials from Local Configuration.
        Note: This method should be explicitly called after constructing new
              object, as in 'explicit is better than implicit'.
        """
        import ConfigParser
        configInfo = {}
        config = ConfigParser.ConfigParser()
        try:
            config.read(CONFIG_PATH)
        except:
            msgerr = "cannot find this file %s" % CONFIG_PATH
            return False, msgerr

        for section in config.sections():
            configInfo[section] = {}

        for section in config.sections():
            for option in config.options(section):
                configInfo[section][option] = config.get(section, option)

        if configInfo:
            try:
                # FIXME
                self.access_key = configInfo["Credentials"]["access_key"]
                self.secret_key = configInfo["Credentials"]["secret_key"]
                self.token = ""
            finally:
                return True
        else:
            msgerr = "empty file %s" % CONFIG_PATH
            return False

    def get_credentials_from_iamrole(self):
        """Read IAM credentials from AWS metadata store.
        Note: This method should be explicitly called after constructing new
              object, as in 'explicit is better than implicit'.
        """
        request = urllib2.Request(
            urlparse.urljoin(
                urlparse.urljoin(
                    "http://169.254.169.254/",
                    "latest/meta-data/iam/security-credentials/",
                ), self.iamrole))

        response = None
        try:
            response = urllib2.urlopen(request)
            data = json.loads(response.read())
        finally:
            if response:
                response.close()

        self.access_key = data['AccessKeyId']
        self.secret_key = data['SecretAccessKey']
        self.token = data['Token']

    def _request(self, baseurl, path):
        path_enc = urllib2.quote(path)
        url = urlparse.urljoin(baseurl, path_enc)
        request = urllib2.Request(url)
        request.add_header('x-amz-security-token', self.token)
        signature = self.sign(request)
        request.add_header('Authorization', "AWS {0}:{1}".format(
            self.access_key,
            signature))
        return request

    def urlgrab(self, url, filename=None, **kwargs):
        """urlgrab(url) copy the file to the local filesystem."""
        for baseurl in self.baseurl:
            request = self._request(baseurl, url)
            if filename is None:
                filename = request.get_selector()
                if filename.startswith('/'):
                    filename = filename[1:]
            response = None
            try:
                out = open(filename, 'w+')
                response = urllib2.urlopen(request)
                buff = response.read(8192)
                while buff:
                    out.write(buff)
                    buff = response.read(8192)
            except:
                continue
            finally:
                if response:
                    response.close()
                out.close()
            if not ".xml" in filename:
                print os.path.basename(filename)
            return filename

    def urlopen(self, url, **kwargs):
        """urlopen(url) open the remote file and return a file object."""
        return urllib2.urlopen(self._request(url))

    def urlread(self, url, limit=None, **kwargs):
        """urlread(url) return the contents of the file as a string."""
        return urllib2.urlopen(self._request(url)).read()

    def sign(self, request):
        """Attach a valid S3 signature to request.
        request - instance of Request
        """
        date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
        request.add_header('Date', date)
        host = request.get_host()

        # TODO: bucket name finding is ugly, I should find a way to support
        # both naming conventions: http://bucket.s3.amazonaws.com/ and
        # http://s3.amazonaws.com/bucket/
        try:
            pos = host.find(".s3")
            assert pos != -1
            bucket = host[:pos]
        except AssertionError:
            raise yum.plugins.PluginYumExit(
                "s3iam: baseurl hostname should be in format: "
                "'<bucket>.s3<aws-region>.amazonaws.com'; "
                "found '%s'" % host)

        resource = "/%s%s" % (bucket, request.get_selector(), )
        amz_headers = 'x-amz-security-token:%s\n' % self.token
        signdict = {'method': request.get_method(),
                    'date': request.headers.get('Date'),
                    'canon_amzn_headers': amz_headers,
                    'canon_amzn_resource': resource}

        sigstring = ("%(method)s\n\n\n%(date)s\n"
                     "%(canon_amzn_headers)s"
                     "%(canon_amzn_resource)s") % (signdict)

        digest = hmac.new(
            str(self.secret_key),
            str(sigstring),
            hashlib.sha1).digest()
        signature = digest.encode('base64')
        return signature
