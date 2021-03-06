"""A notebook manager that uses SHOCK storage.

Authors:

* Travis Harrison
"""

#-----------------------------------------------------------------------------
#  Copyright (C) 2012  The IPython Development Team
#
#  Distributed under the terms of the BSD License.  The full license is in
#  the file COPYING, distributed as part of this software.
#-----------------------------------------------------------------------------

#-----------------------------------------------------------------------------
# Imports
#-----------------------------------------------------------------------------

import cStringIO
import datetime
import requests, json
import dateutil.parser

from collections import defaultdict
from tornado import web

from .nbmanager import NotebookManager
from IPython.nbformat import current
from IPython.utils.traitlets import Unicode, Instance


#-----------------------------------------------------------------------------
# Classes
#-----------------------------------------------------------------------------

class ShockNotebookManager(NotebookManager):

    shock_url  = Unicode('', config=True, help='SHOCK server url')
    shock_user = Unicode('', config=True, help='SHOCK user name.')
    shock_map  = {}

    def load_notebook_names(self):
        """On startup load the notebook ids and names from SHOCK.
        The uuid and name are stored as shock metadata.
            1. Skip nb nodes with no files
            2. Skip nb nodes tagged as 'deleted'
            3. If multiple nb with same uuid, get latest timestamp
        """
        self.mapping = {}
        self.shock_map = {}
        nb_vers = defaultdict(list)
        
        query_url = self.shock_url+'/node?query&type=ipynb'
        if self.shock_user:
            query_url += '&user='+self.shock_user
        query_res = self.getShock(query_url, 'json')
        
        if query_res is not None:
            for node in query_res:
                if (node['file']['size'] == 0) or ('deleted' in node['attributes']):
                    continue
                nb_vers[ node['attributes']['uuid'] ].append(node)

        for uuid in nb_vers.iterkeys():
            nodes = sorted(nb_vers[uuid], key=lambda x: x['attributes']['created'], reverse=True)
            self.mapping[uuid] = nodes[0]['attributes']['name']
            self.shock_map[uuid] = nodes[0]

    def list_notebooks(self):
        """List all notebooks in the container.
        This version uses `self.mapping` as the authoritative notebook list.
        """
        data = [dict(notebook_id=id,name=name) for id, name in self.mapping.items()]
        data = sorted(data, key=lambda item: item['name'])
        return data

    def delete_notebook_id(self, notebook_id):
        """Delete a notebook's id in the mapping.
        This doesn't delete the actual notebook, only its entry in the mapping.
        """
        del self.mapping[notebook_id]
        del self.shock_map[notebook_id]

    def notebook_exists(self, notebook_id):
        """Does a notebook exist?"""
        if (notebook_id in self.mapping) and (notebook_id in self.shock_map):
            return True
        else:
            return False

    def read_notebook_object(self, notebook_id):
        """Get the object representation of a notebook by notebook_id."""
        if not self.notebook_exists(notebook_id):
            raise web.HTTPError(404, u'Notebook does not exist: %s' %notebook_id)
        try:
            node_url  = '%s/node/%s?download' %(self.shock_url, self.shock_map[notebook_id]['id'])
            node_data = self.getShock(node_url, 'data')
        except:
            raise web.HTTPError(500, u'Notebook cannot be read')
        try:
            # v1 and v2 and json in the .ipynb files.
            nb = current.reads(node_data, u'json')
        except:
            raise web.HTTPError(500, u'Unreadable JSON notebook.\n%s' %node_data)
        dt = self.shock_map[notebook_id]['attributes']['created']
        last_modified = dateutil.parser.parse(dt) if dt else datetime.datetime.now().isoformat()
        return last_modified, nb

    def write_notebook_object(self, nb, notebook_id=None):
        """Save an existing notebook object by notebook_id."""
        try:
            new_name = nb.metadata.name
        except AttributeError:
            raise web.HTTPError(400, u'Missing notebook name')
        try:
            if notebook_id is None:
                notebook_id = self.new_notebook_id(new_name)
            nb.metadata.created = datetime.datetime.now().isoformat()
            nb.metadata.user = self.shock_user if self.shock_user else 'public'
            nb.metadata.type = 'ipynb'
            nb.metadata.uuid = notebook_id
        except Exception as e:
            raise web.HTTPError(400, u'Unexpected error setting notebook attributes: %s' %e)
        if notebook_id not in self.mapping:
            raise web.HTTPError(404, u'Notebook does not exist: %s' %notebook_id)

        try:
            data = current.writes(nb, u'json')
            attr = json.dumps(nb.metadata)
            shock_node = self.postShock(self.shock_url+'/node', new_name, data, attr)
        except Exception as e:
            raise web.HTTPError(400, u'Unexpected error while saving notebook: %s' %e)

        self.mapping[notebook_id] = new_name
        self.shock_map[notebook_id] = shock_node
        return notebook_id

    def delete_notebook(self, notebook_id):
        """Delete notebook by notebook_id.
        Currently can not delete or change data in shock"""
        if not self.notebook_exists(notebook_id):
            raise web.HTTPError(404, u'Notebook does not exist: %s' %notebook_id)
        self.delete_notebook_id(notebook_id)

    def getShock(self, url, format):
        content = None
        try:
            rget = requests.get(url)
        except Exception as e:
            raise web.HTTPError(400, u'Unable to connect to SHOCK server %s: %s' %(url, e))
        if not (rget.ok and rget.text):
            raise web.HTTPError(400, u'Unable to connect to SHOCK server %s: %s' %(url, rget.raise_for_status()))
        if format == 'json':
            rj = rget.json
            if not (rj and isinstance(rj, dict) and all([key in rj for key in ['S','D','E']])):
                raise web.HTTPError(415, u'Return data not valid SHOCK format: %s' %e)
            if rj['E']:
                raise web.HTTPError(rj['S'], 'SHOCK error: '+rj['E'])
            return rj['D']
        else:
            return rget.text

    def postShock(self, url, name, data, attr):
        dataHdl = cStringIO.StringIO(data)
        attrHdl = cStringIO.StringIO(attr)
        files = { "file": ('%s.ipynb'%name, dataHdl), "attributes": ('%s_metadata.json'%name, attrHdl) }
        try:
            rpost = requests.post(url, files=files)
            rj = rpost.json
        except Exception as e:
            raise web.HTTPError(400, u'Unable to connect to SHOCK server %s: %s' %(url, e))
        if not (rpost.ok and rj and isinstance(rj, dict) and all([key in rj for key in ['S','D','E']])):
            raise web.HTTPError(400, u'Unable to POST to SHOCK server %s: %s' %(url, rpost.raise_for_status()))
        if rj['E']:
            raise web.HTTPError(rj['S'], 'SHOCK error: '+rj['E'])
        return rj['D']

    def log_info(self):
        self.log.info("Serving notebooks from SHOCK storage for user %s: %s" %(self.shock_user, self.shock_url))
