#!/usr/bin/python
# Copyright (C) 2006-2007 XenSource Ltd.
# Copyright (C) 2008-2009 Citrix Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published
# by the Free Software Foundation; version 2.1 only.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# FileSR: local-file storage repository

import SR, VDI, SRCommand, NFSSR, FileSR, util
import errno
import os, re, sys
import xml.dom.minidom
import xmlrpclib
import xs_errors
import nfs
import vhdutil
from lock import Lock
import cleanup

CAPABILITIES = ["SR_PROBE","SR_UPDATE", "SR_CACHING",
                "VDI_CREATE","VDI_DELETE","VDI_ATTACH","VDI_DETACH",
                "VDI_UPDATE", "VDI_CLONE","VDI_SNAPSHOT","VDI_RESIZE",
                "VDI_GENERATE_CONFIG",
                "VDI_RESET_ON_BOOT/2", "ATOMIC_PAUSE"]

CONFIGURATION = [ [ 'server', 'hostname or IP address of CIFS server (required)' ], \
                  [ 'username', 'The username to be used during CIFS authentication' ], \
                  [ 'password', 'The password to be used during CIFS authentication' ] ]

DRIVER_INFO = {
    'name': 'CIFS VHD',
    'description': 'SR plugin which stores disks as VHD files on a remote CIFS filesystem',
    'vendor': 'Citrix Systems Inc',
    'copyright': '(C) 2008 Citrix Systems Inc',
    'driver_version': '1.0',
    'required_api_version': '1.0',
    'capabilities': CAPABILITIES,
    'configuration': CONFIGURATION
    }

DRIVER_CONFIG = {"ATTACH_FROM_CONFIG_WITH_TAPDISK": True}

# The mountpoint for the directory when performing an sr_probe.  All probes
# are guaranteed to be serialised by xapi, so this single mountpoint is fine.

# server = //server/vol1 - ie the export path on the array
# serverpath = /VMs or '' - the subdir on the array below the export path
# 
# remoteserver == server
# mountpoint = /var/run/sr-mount/CIFS/<cifs_server_name>/<share_name>/uuid
# path = /var/run/sr-mount/uuid
# linkpath = mountpoint/uuid 

class CIFSSR(FileSR.FileSR):
    """CIFS file-based storage repository"""
    def handles(type):
        return type == 'cifs'
    handles = staticmethod(handles)

    def load(self, sr_uuid):
        self.ops_exclusive = FileSR.OPS_EXCLUSIVE
        self.lock = Lock(vhdutil.LOCK_TYPE_SR, self.uuid)
        self.sr_vditype = SR.DEFAULT_TAP
        self.driver_config = DRIVER_CONFIG
        if not self.dconf.has_key('server'):
            raise xs_errors.XenError('ConfigServerMissing')
        self.remoteserver = self.dconf['server']
        self.nosubdir = False
        if self.sr_ref and self.session is not None :
            self.sm_config = self.session.xenapi.SR.get_sm_config(self.sr_ref)
        else:
            self.sm_config = self.srcmd.params.get('sr_sm_config') or {}
        self.nosubdir = self.sm_config.get('nosubdir') == "true"
        self.credentials = None
        self.mountpoint = os.path.join(SR.MOUNT_BASE, 'CIFS', self._extract_server(), sr_uuid)
        self.linkpath = os.path.join(self.mountpoint, 
                                           not self.nosubdir and sr_uuid or "")
        self.path = os.path.join(SR.MOUNT_BASE, sr_uuid)
        self._check_o_direct()

    def _checkmount(self):
        return util.ioretry(lambda: ((util.pathexists(self.mountpoint) and \
				util.ismount(self.mountpoint)) and \
                                util.pathexists(self.linkpath)))


    def mount(self):
        """Mount the remote CIFS export at 'mountpoint'"""
        try:
            if not util.ioretry(lambda: util.isdir(self.mountpoint)):
                util.ioretry(lambda: util.makedirs(self.mountpoint))
        except util.CommandException, inst:
            raise nfs.NfsException("Failed to make directory: code is %d" %
                                inst.code)

        options = 'sec=ntlm'
        options += ',cache=none'

        self.credentials = os.path.join("/tmp", util.gen_uuid())

        if self.dconf.has_key('username') \
                and self.dconf.has_key('password'):
            username = self.dconf['username'].replace("\\","/")
            password = self.dconf['password']

            username = util.to_plain_string(username)
            password = util.to_plain_string(password)

            # Open credentials file and truncate
            f = open(self.credentials, 'w')
            f.write("username=%s\npassword=%s\n" % (username,password))
            f.close()

            options += ',credentials=%s' % self.credentials
    
        try:
            util.ioretry(lambda:
                util.pread(["mount.cifs", self.remoteserver,
                self.mountpoint, "-o", options]),
                errlist=[errno.EPIPE, errno.EIO],
                maxretry=2, nofail=True)
        except util.CommandException, inst:
            raise nfs.NfsException("mount failed with return code %d" % inst.code)

    def unmount(self, mountpoint, rmmountpoint):
        """Unmount the remote CIFS export at 'mountpoint'"""
        try:
            util.pread(["umount", mountpoint])
        except util.CommandException, inst:
            raise nfs.NfsException("umount failed with return code %d" % inst.code)

        if rmmountpoint:
            try:
                os.rmdir(mountpoint)
            except OSError, inst:
                raise nfs.NfsException("rmdir failed with error '%s'" % inst.strerror)


    def _extract_server(self):
        return self.remoteserver[2:]

    def _check_license(self):
        """Raises an exception if CIFS is not licensed."""
        if self.session is None or (isinstance(self.session, str) and \
                self.session == ""):
            raise xs_errors.XenError('NoCifsLicense',
                    'No session object to talk to XAPI')
        restrictions = util.get_pool_restrictions(self.session)
        if 'restrict_cifs' in restrictions and \
                restrictions['restrict_cifs'] == "true":
            raise xs_errors.XenError('NoCifsLicense')


    def attach(self, sr_uuid):
        if not self._checkmount():
            self.mount()
            os.symlink(self.linkpath, self.path)
        self.attached = True


    def probe(self):
        pass

    def detach(self, sr_uuid):
        """Detach the SR: Unmounts and removes the mountpoint"""
        if not self._checkmount():
            return
        util.SMlog("Aborting GC/coalesce")
        cleanup.abort(self.uuid)

        # Change directory to avoid unmount conflicts
        os.chdir(SR.MOUNT_BASE)

        try:
            os.unlink(self.path)
            self.unmount(self.mountpoint, True)
        except nfs.NfsException, exc:
            raise xs_errors.XenError('NFSUnMount', opterr=exc.errstr)

        self.attached = False

        
    def create(self, sr_uuid, size):
        self._check_license()

        if self._checkmount():
            raise xs_errors.XenError('NFSAttached')

        try:
            self.mount() 
        except Exception, exn:
            try:
                os.rmdir(self.mountpoint)
            except:
                pass
            raise exn

        if util.ioretry(lambda: util.pathexists(self.linkpath)):
            if len(util.ioretry(lambda: util.listdir(self.linkpath))) != 0:
                self.detach(sr_uuid)
                raise xs_errors.XenError('SRExists')
        else:
            try:
                util.ioretry(lambda: util.makedirs(self.linkpath))
                os.symlink(self.linkpath, self.path)
            except util.CommandException, inst:
                if inst.code != errno.EEXIST:
                    self.detach(sr_uuid)
                    raise xs_errors.XenError('NFSCreate',
                        opterr='remote directory creation error is %d'
                        % inst.code)
        self.detach(sr_uuid)

    def delete(self, sr_uuid):
        # try to remove/delete non VDI contents first
        super(CIFSSR, self).delete(sr_uuid)
        try:
            if self._checkmount():
                self.detach(sr_uuid)

            self.mount()
            if util.ioretry(lambda: util.pathexists(self.linkpath)):
                util.ioretry(lambda: os.rmdir(self.linkpath))
            self.unmount(self.mountpoint, True)
        except util.CommandException, inst:
            self.detach(sr_uuid)
            if inst.code != errno.ENOENT:
                raise xs_errors.XenError('NFSDelete')

    def vdi(self, uuid, loadLocked = False):
        if not loadLocked:
            return CIFSFileVDI(self, uuid)
        return CIFSFileVDI(self, uuid)
    
class CIFSFileVDI(FileSR.FileVDI):
    def attach(self, sr_uuid, vdi_uuid):
        if not hasattr(self,'xenstore_data'):
            self.xenstore_data = {}
            
        self.xenstore_data["storage-type"]="nfs"

        return super(CIFSFileVDI, self).attach(sr_uuid, vdi_uuid)

    def generate_config(self, sr_uuid, vdi_uuid):
        util.SMlog("CIFSFileVDI.generate_config")
        if not util.pathexists(self.path):
                raise xs_errors.XenError('VDIUnavailable')
        resp = {}
        resp['device_config'] = self.sr.dconf
        resp['sr_uuid'] = sr_uuid
        resp['vdi_uuid'] = vdi_uuid
        resp['sr_sm_config'] = self.sr.sm_config
        resp['command'] = 'vdi_attach_from_config'
        # Return the 'config' encoded within a normal XMLRPC response so that
        # we can use the regular response/error parsing code.
        config = xmlrpclib.dumps(tuple([resp]), "vdi_attach_from_config")
        return xmlrpclib.dumps((config,), "", True)

    def attach_from_config(self, sr_uuid, vdi_uuid):
        """Used for HA State-file only. Will not just attach the VDI but
        also start a tapdisk on the file"""
        util.SMlog("CIFSFileVDI.attach_from_config")
        try:
            if not util.pathexists(self.sr.path):
                self.sr.attach(sr_uuid)
        except:
            util.logException("NFSFileVDI.attach_from_config")
            raise xs_errors.XenError('SRUnavailable', \
                        opterr='Unable to attach from config')


if __name__ == '__main__':
    SRCommand.run(CIFSSR, DRIVER_INFO)
else:
    SR.registerSR(CIFSSR)
#
