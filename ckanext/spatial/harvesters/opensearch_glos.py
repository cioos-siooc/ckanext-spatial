from __future__ import print_function

import six
from six.moves.urllib.parse import urljoin
# from six.moves import html_parser
import logging
import hashlib
import re

import datetime
# import dateutil.parser
import pyparsing as parse
import requests
from sqlalchemy.orm import aliased
from sqlalchemy.exc import DataError

from ckan import model
from ckan.lib.helpers import json
from ckan.logic import ValidationError, NotFound, get_action

from ckan.plugins.core import SingletonPlugin, implements
import ckan.plugins.toolkit as toolkit
from ckantoolkit import config

from ckanext.harvest.interfaces import IHarvester
from ckanext.harvest.model import HarvestObject
from ckanext.harvest.model import HarvestObjectExtra as HOExtra
import ckanext.harvest.queue as queue
from ckanext.harvest.queue import get_connection_redis

from ckanext.spatial.harvesters.base import SpatialHarvester, guess_standard

import boto3
import unicodedata

log = logging.getLogger(__name__)


class GLOSHarvester(SpatialHarvester, SingletonPlugin):
    '''
    A Harvester for WAF (Web Accessible Folders) containing spatial metadata documents.
    e.g. Apache serving a directory of ISO 19139 files.
    '''

    implements(IHarvester)

    redis_translation_store = 'awsTranslations_glos'
    translation_method_text = "text translated using the Amazon translate service / texte traduit à l'aide du service Amazon translate"

    def info(self):
        return {
            'name': 'GLOS iso',
            'title': 'Harvester for GLOS Catalogue ISO xml',
            'description': 'geoportal rest api lists datasets urls with avilable iso19115-2 xml'
            }

    def translate_string(self, redis_conn, string_to_translate, source_lang='en', target_lang='fr'):
        store_name = '%s_%s_to_%s' % (self.redis_translation_store,source_lang,target_lang)
        # check for string in redis
        redis_trans = redis_conn.hget(store_name,string_to_translate)
        if redis_trans: 
            # replace non-breaking white space
            redis_trans = redis_trans.replace(u'\u00A0',' ')
            log.debug('"%s" found in cache', string_to_translate)
            return redis_trans

        # if not exists, call aws translate
        try:      
            translate = boto3.client(service_name='translate', use_ssl=True)
            aws_trans_obj = translate.translate_text(Text=string_to_translate, SourceLanguageCode=source_lang, TargetLanguageCode=target_lang)
            aws_trans = aws_trans_obj.get('TranslatedText')
            # replace non-breaking white space
            aws_trans = aws_trans.replace(u'\u00A0',' ')
            
            # save translation to redis
            if aws_trans:
                log.debug('"%s" saved to cache', string_to_translate)
                redis_conn.hset(store_name, mapping={string_to_translate:aws_trans})
                return aws_trans
        except Exception as e:
              log.error('Could not translate text %s : %e', string_to_translate, e)

        return None

    def get_package_dict(self, iso_values, harvest_object):
        package_dict = super(GLOSHarvester, self).get_package_dict(iso_values, harvest_object)


        # setup redis connection so we can check redis for translation, call Amazon translate 
        # if needed and cache results in redis
        redis_conn = get_connection_redis()
   
        package_dict["eov"] = ["other"]

        id = iso_values["unique-resource-identifier-full"]
        if id:
            #load citation values and change url to point to seagull erddap server
            citation = toolkit.h.cioos_load_json(iso_values["citation"])           
            en = toolkit.h.cioos_load_json(toolkit.h.cioos_load_json(toolkit.h.cioos_load_json(citation['en'].replace('\\"', '\"'))))
            fr = toolkit.h.cioos_load_json(toolkit.h.cioos_load_json(toolkit.h.cioos_load_json(citation['fr'].replace('\\"', '\"'))))
            log.debug('CITATION_EN: %r',en)
            en0 = toolkit.h.cioos_load_json(en[0])
            fr0 = toolkit.h.cioos_load_json(fr[0])
            log.debug('CITATION_EN: %r',en0)
            en0['URL'] = 'https://%s/erddap/%s/info/%s/index.html' % (id['authority'], 'en', id['code'])
            fr0['URL'] = 'https://%s/erddap/%s/info/%s/index.html' % (id['authority'], 'fr', id['code'])
            citation['en'] = json.dumps([en0]).replace('\"', '\\"')
            citation['fr'] = json.dumps([fr0]).replace('\"', '\\"')
            iso_values["citation"] = json.dumps(citation)

            # GLOS uses authority instead of code-space to store the domain name
            id['code-space'] = id['authority']
            id['authority'] = 'GLOS'
            iso_values["unique-resource-identifier-full"] = id

        # Keywords auto translated
        # in some cases there are no keywords at all
        if iso_values.get('keywords'):
            for item in iso_values['keywords']:
                keyword = json.loads(item.get('keyword','{}'))  
                en_string = None  
                fr_string = None    
                if isinstance(keyword, dict):
                    en_string = keyword.get('en')
                    fr_string = keyword.get('fr')
                else:
                    en_string = keyword

                if en_string and not fr_string:
                    en_string = en_string.replace('"','')
                    en_string = unicodedata.normalize("NFKD", en_string)
                    fr_string = self.translate_string(redis_conn, en_string, 'en', 'fr')
                    item['keyword'] = '{"en": "%s", "fr": "%s"}' % (en_string,fr_string)
                    package_dict['keywords_translation_method'] = json.dumps({'en':'', 'fr':'Keyword ' + self.translation_method_text})
                elif fr_string and not en_string:
                    fr_string = fr_string.replace('"','')
                    fr_string = unicodedata.normalize("NFKD", fr_string)
                    en_string = self.translate_string(redis_conn, fr_string, 'fr', 'en')
                    item['keyword'] = '{"en": "%s", "fr": "%s"}' % (en_string,fr_string)
                    package_dict['keywords_translation_method'] = json.dumps({'fr':'', 'en':'Keyword ' + self.translation_method_text})
        else:
            iso_values['keywords'] = [{'keyword': '{"en": "other", "fr": "autre"}', 'type': ''}]

        # Title auto translated
        title = json.loads(package_dict["title"])
        if title.get('en') and not title.get('fr'):
            title['fr'] = self.translate_string(redis_conn, title['en'] , 'en', 'fr')
            package_dict["title"] = json.dumps(title)
            package_dict['title_translation_method'] = json.dumps({'en':'', 'fr':'Title ' + self.translation_method_text})
        elif title.get('fr') and not title.get('en'):
            title['en'] = self.translate_string(redis_conn, title['fr'] , 'fr', 'en')
            package_dict["title"] = json.dumps(title)
            package_dict['title_translation_method'] = json.dumps({'fr':'', 'en':'Title ' + self.translation_method_text})

        # Description auto translated
        notes = json.loads(package_dict["notes"])
        if notes.get('en') and not notes.get('fr'):
            notes['fr'] = self.translate_string(redis_conn, notes['en'] , 'en', 'fr')
            package_dict["notes"] = json.dumps(notes)
            package_dict['notes_translation_method'] = json.dumps({'en':'', 'fr':'Description ' + self.translation_method_text})
        elif notes.get('fr') and not notes.get('en'):
            notes['en'] = self.translate_string(redis_conn, notes['fr'] , 'fr', 'en')
            package_dict["notes"] = json.dumps(notes)
            package_dict['notes_translation_method'] = json.dumps({'fr':'', 'en':'Description ' + self.translation_method_text})

        # End of processing, return the modified package
        return package_dict

    def search_for_datasets(self, source_url, get_changes_since, harvest_job):
        start = 1
        params = {'f':'json', 'sort':'id', 'start': str(start), 'num':'100', 'modified':'{0}/*'.format(get_changes_since or '*')}
        #params = {'f':'json', 'sort':'id', 'modified':'2023-06-11T00:10:48.35/2023-06-11T00:10:48.36'}
        
        datasets = []
        if get_changes_since:
            log.info('Searching for datasets modified since: %s UTC', get_changes_since)

        while int(params['start']) > 0:
            # Get contents
            url = source_url
            try:
                log.info('Requesting %s %s', url, params)
                response = requests.get(url, params=params, timeout=60)
                response.encoding = 'UTF-8'
                response.raise_for_status()
            except requests.exceptions.RequestException as e:
                if e.response.status_code == 404:
                    return datasets
                self._save_gather_error('Unable to get content for URL: %s: %r' %
                                        (url, e), harvest_job)
                return None
            content = response.json()
            

            for result in content['results']:
                source_obj = result['_source']
                datestamp = source_obj.get('sys_xmlmodified_dt') or source_obj.get('sys_created_dt') or ''
                datasets.append(
                    {
                    'id': result['id'],
                    'xml': source_obj['sys_xml_clob'],
                    'datestamp': datestamp.replace('Z','+0000'),
                    'url': 'https://seagull-geoportal.glos.org/geoportal/rest/metadata/item/%s/xml' % result['id']
                    }
                )
 
            log.debug('Datasets: %r', datasets)
            params['start'] = content['nextStart']
        return datasets
  

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


        last_error_free_job = self.last_error_free_job(harvest_job)
        log.debug('Last error-free job: %r', last_error_free_job)
        get_changes_since = None
        if (last_error_free_job and not self.source_config.get('force_all', False)):
            # Request only the datasets modified since
            last_time = last_error_free_job.gather_started
            # Note: SOLR works in UTC, and gather_started is also UTC, so
            # this should work as long as local and remote clocks are
            # relatively accurate. Going back a little earlier, just in case.
            get_changes_since = \
                (last_time - datetime.timedelta(hours=1)).isoformat()

        # source_url = 'https://seagull-geoportal.glos.org/geoportal/opensearch'
        responses = self.search_for_datasets(source_url, get_changes_since, harvest_job)
        harvest_response_dict = {x['url']: x for x in responses} # mapping of url harvest content
      

        ######  Compare source and db ######

        harvest_locations = set(harvest_response_dict.keys())
        old_locations = set(url_to_modified_db.keys())

        new = harvest_locations - old_locations
        if self.source_config.get('force_all', False):
            delete =  old_locations - harvest_locations
        else:
            delete = []
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

            # Remove original XML declaration
            content = re.sub('<\?xml(.*)\?>', '', content)

            # Get rid of the BOM and other rubbish at the beginning of the file
            content = re.sub('.*?<', '<', content, 1)
            content = content[content.index('<'):]

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
                                                     [location]['datestamp'],
                                                     'new'),
                                guid=guid
                               )

            obj.add()
            obj = add_content(harvest_response_dict[location]['xml'], obj)
            obj.save()
            ids.append(obj.id)

        for location in change:
            obj = HarvestObject(job=harvest_job,
                                extras=create_extras(location,
                                                     harvest_response_dict[location]['datestamp'],
                                                     'change'),
                                guid=url_to_ids[location][0],
                                package_id=url_to_ids[location][1]
                               )
            obj.add()
            obj = add_content(harvest_response_dict[location]['xml'], obj)
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