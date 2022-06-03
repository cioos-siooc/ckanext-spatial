from __future__ import print_function

import six
from six.moves.urllib.parse import urljoin
from six.moves import html_parser
import logging
import hashlib

import dateutil.parser
import pyparsing as parse
import requests
from sqlalchemy.orm import aliased
from sqlalchemy.exc import DataError

from ckan import model
from ckan.lib.helpers import json
from ckan.logic import ValidationError, NotFound, get_action

from ckan.plugins.core import SingletonPlugin, implements
from ckantoolkit import config

from ckanext.harvest.interfaces import IHarvester
from ckanext.harvest.model import HarvestObject
from ckanext.harvest.model import HarvestObjectExtra as HOExtra
import ckanext.harvest.queue as queue

from ckanext.spatial.harvesters.base import SpatialHarvester, guess_standard

from lxml import etree

log = logging.getLogger(__name__)


class PolarHarvester(SpatialHarvester, SingletonPlugin):
    '''
    A Harvester for WAF (Web Accessible Folders) containing spatial metadata documents.
    e.g. Apache serving a directory of ISO 19139 files.
    '''

    implements(IHarvester)

    def info(self):
        return {
            'name': 'polar iso',
            'title': 'Harvester for Polar Data Catalogue ISO xml',
            'description': 'api lists datasets urls with avilable iso19115-1 xml'
            }

    def get_package_dict(self, iso_values, harvest_object):
        package_dict = super(PolarHarvester, self).get_package_dict(iso_values, harvest_object)

        iso_values["citation"] = '{"fr": "%s", "en": "%s"}' % (iso_values.get('citation-other', ''), iso_values.get('citation-other', ''))
        package_dict["eov"] = ["other"]

        if iso_values.get('keywords'):
            iso_values['keywords'].append({'keyword': '{"fr": "autre"}', 'type': ''})
        else:
            iso_values['keywords'] = [{'keyword': '{"en": "other"}', 'type': ''}, {'keyword': '{"fr": "autre"}', 'type': ''}]

        title = json.loads(package_dict["title"])
        title['fr'] = 'none'
        package_dict["title"] = json.dumps(title)

        notes = json.loads(package_dict["notes"])
        notes['fr'] = 'none'
        package_dict["notes"] = json.dumps(notes)

        try:
            if iso_values['temporal-extent']['end'] == 'Undefined':
                iso_values['temporal-extent']['end'] = ''
        except Exception:
            pass

        # fix some role code errors.
        # TODO: check if this is fixed in original data yet?
        for c in iso_values.get("cited-responsible-party", []):
            if c:
                if c['role'] == 'Originator':
                    c['role'] = 'originator'
                elif c['role'] == 'Collaborator':
                    c['role'] = 'collaborator'
                elif c['role'] == 'ri_419':
                    c['role'] = 'collaborator'

        # polar data centre does not provide a link to there data in most cases.
        # This block provides an email address to contact distributor if set
        resources = []
        if not iso_values.get('resource-locator'):
            for d in iso_values.get('distributor', []):
                resources.append({
                    'url': 'mailto:' + d.get('contact-info', {}).get('email', ''),
                    'name': 'Contact Distributor for more information',
                    'description': ' - '.join([d.get('individual-name'), d.get('organisation-name')]),
                    'resource_locator_protocol': '',
                    'resource_locator_function': 'information'
                })

        package_dict['resources'] = resources

        # End of processing, return the modified package
        return package_dict

    def search_for_datasets(self, source_url, harvest_job):
        source_url = 'http://hedeby.uwaterloo.ca/api/metadata/xml/'
        page = 1
        params = {'page': str(page)}
        xml = []
        while True:
            # Get contents
            url = source_url + str(page)
            try:
                log.info('Requesting %s', url)
                response = requests.get(url, timeout=60)
                response.raise_for_status()
            except requests.exceptions.RequestException as e:
                if e.response.status_code == 404:
                    return xml
                self._save_gather_error('Unable to get content for URL: %s: %r' %
                                        (url, e), harvest_job)
                return None

            content = response.text
            tree = etree.fromstring(str.encode(content))

            xml_list = []

            for n in tree.findall(".//{http://www.isotc211.org/2005/gmd}MD_Metadata"):
                file_id = n.find("./{http://www.isotc211.org/2005/gmd}fileIdentifier/{http://www.isotc211.org/2005/gco}CharacterString")
                datestamp = n.find("./{http://www.isotc211.org/2005/gmd}dateStamp/{http://www.isotc211.org/2005/gco}Date")
                xml_list.append({'url': "https://polardata.ca/pdcsearch/xml/iso/" + file_id.text, 'xml': etree.tostring(n, encoding='unicode', method='xml'), 'date': datestamp.text})

            if len(xml_list) == 0:
                return xml
            xml = xml + xml_list

            page = page + 1

            params['page'] = str(page)

    def fetch_stage(self, harvest_object):
        # nothing else to do as content is already fetched in gather stage.
        return True

    def gather_stage(self, harvest_job, collection_package_id=None):
        log = logging.getLogger(__name__ + '.WAF.gather')

        self.harvest_job = harvest_job

        # Get source URL
        source_url = harvest_job.source.url

        self._set_source_config(harvest_job.source.config)

        ######  Get current harvest object out of db ######

        url_to_modified_db = {}  # mapping of url to last_modified in db
        url_to_ids = {}  # mapping of url to guid in db

        HOExtraAlias1 = aliased(HOExtra)
        HOExtraAlias2 = aliased(HOExtra)
        query = model.Session.query(HarvestObject.guid, HarvestObject.package_id, HOExtraAlias1.value, HOExtraAlias2.value).\
                        join(HOExtraAlias1, HarvestObject.extras).\
                        join(HOExtraAlias2, HarvestObject.extras).\
                        filter(HOExtraAlias1.key == 'waf_modified_date').\
                        filter(HOExtraAlias2.key == 'waf_location').\
                        filter(HarvestObject.current == True).\
                        filter(HarvestObject.harvest_source_id == harvest_job.source.id)


        for guid, package_id, modified_date, url in query:
            url_to_modified_db[url] = modified_date
            url_to_ids[url] = (guid, package_id)

        ######  Get current list of records from source ######

        # TODO: add check to api/metadata/xml/since/{date} to find datasets that have changed.

        url = 'hedeby.uwaterloo.ca//api/metadata/xml'
        response = self.search_for_datasets(url, harvest_job)

        url_to_modified_harvest = {}  # mapping of url to last_modified in harvest
        xml_datasets = {}
        for n in response:
            url_to_modified_harvest[n['url']] = n['date']
            xml_datasets[n['url']] = n['xml']

        ######  Compare source and db ######

        harvest_locations = set(url_to_modified_harvest.keys())
        old_locations = set(url_to_modified_db.keys())

        new = harvest_locations - old_locations
        delete = old_locations - harvest_locations
        change = old_locations & harvest_locations

        def create_extras(url, date, status):
            extras = [HOExtra(key='waf_modified_date', value=date),
                      HOExtra(key='waf_location', value=url),
                      HOExtra(key='status', value=status)]
            if collection_package_id:
                extras.append(
                    HOExtra(key='collection_package_id',
                            value=collection_package_id)
                )
            return extras


        def add_content(content, harvest_object):
            # Check if it is an ISO document
            document_format = guess_standard(content)
            if document_format == 'iso':
                harvest_object.content = content
            else:
                extra = HOExtra(
                        object=harvest_object,
                        key='original_document',
                        value=content)
                extra.save()

                extra = HOExtra(
                        object=harvest_object,
                        key='original_format',
                        value=document_format)
                extra.save()
            return harvest_object

        ids = []
        for location in new:
            guid=hashlib.md5(location.encode('utf8', 'ignore')).hexdigest()
            obj = HarvestObject(job=harvest_job,
                                extras=create_extras(location,
                                                     url_to_modified_harvest[location],
                                                     'new'),
                                guid=guid
                               )

            obj.add()
            obj = add_content(xml_datasets[location], obj)
            obj.save()
            ids.append(obj.id)

        for location in change:
            obj = HarvestObject(job=harvest_job,
                                extras=create_extras(location,
                                                     url_to_modified_harvest[location],
                                                     'change'),
                                guid=url_to_ids[location][0],
                                package_id=url_to_ids[location][1]
                               )
            obj.add()
            obj = add_content(xml_datasets[location], obj)
            obj.save()
            ids.append(obj.id)

        for location in delete:
            obj = HarvestObject(job=harvest_job,
                                extras=create_extras('', '', 'delete'),
                                guid=url_to_ids[location][0],
                                package_id=url_to_ids[location][1],
                               )
            model.Session.query(HarvestObject).\
                  filter_by(guid=url_to_ids[location][0]).\
                  update({'current': False}, False)

            obj.save()
            ids.append(obj.id)

        if len(ids) > 0:
            log.debug('{0} objects sent to the next stage: {1} new, {2} change, {3} delete'.format(
                len(ids), len(new), len(change), len(delete)))
            return ids
        else:
            if config.get('ckan.harvest.status_mail.all', False):
                self._save_gather_error('No records to change',
                                         harvest_job)
            else:
                log.debug('No records to change')
            return []
