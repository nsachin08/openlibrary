from __future__ import absolute_import

import urllib
import urllib2
import web
import simplejson
import six

from infogami.utils import delegate

from openlibrary.utils import dateutil
from openlibrary.core.helpers import get_coverstore_url
from openlibrary.core.helpers import bookreader_host
from openlibrary.core.lending import config_http_request_timeout


PRESET_QUERIES = {
    'preset:modern': 'languageSorter:"English" AND (year:"2013" OR year:"2014" OR year:"2015")',
    'preset:thrillers': '(creator:"Clancy, Tom" OR creator:"King, Stephen" OR creator:"Clive Cussler" OR creator:("Cussler, Clive") OR creator:("Dean Koontz") OR creator:("Koontz, Dean") OR creator:("Higgins, Jack")) AND !publisher:"Pleasantville, N.Y. : Reader\'s Digest Association" AND languageSorter:"English"',
    'preset:children': '(creator:("parish, Peggy") OR creator:("avi") OR title:("goosebumps") OR creator:("Dahl, Roald") OR creator:("ahlberg, allan") OR creator:("Seuss, Dr") OR creator:("Carle, Eric") OR creator:("Pilkey, Dav"))',
    'preset:comics': '(subject:"comics" OR creator:("Gary Larson") OR creator:("Larson, Gary") OR creator:("Charles M Schulz") OR creator:("Schulz, Charles M") OR creator:("Jim Davis") OR creator:("Davis, Jim") OR creator:("Bill Watterson") OR creator:("Watterson, Bill") OR creator:("Lee, Stan"))',
    'preset:authorsalliance_mitpress': '(openlibrary_subject:(authorsalliance) OR collection:(mitpress) OR publisher:(MIT Press) OR openlibrary_subject:(mitpress)) AND (!loans__status__status:UNAVAILABLE)'
}

# Advanced Search internal calls use http for debugging
SEARCH_URL = 'http://%s/advancedsearch.php' % bookreader_host()
BROWSE_URL = 'https://%s/search.php' % bookreader_host()

AVAILABILITY_STATUS = 'loans__status__status'
RETURN_FIELDS = ['identifier', AVAILABILITY_STATUS,
                 'openlibrary_edition', 'openlibrary_work']
MAX_IA_RESULTS = 100


def editions_by_ia_query(query='', sorts=None, page=1, limit=None):
    """This method allows Open Library to fetch editions from its catalog
    by using the Archive.org Advanced Search as the querying
    mechanism. This is advantageous because Archive.org has the latest
    book availability information.

    Retrieves a list of available editions (one per work) on Open
    Library using the archive.org advancedsearch API. Is used in such
    components as IABookCarousel to retrieve a list of unique
    available books.

    """
    # Enable method to be cacheable
    if 'env' not in web.ctx:
        delegate.fakeload()

    q = _prepare_api_query(query)
    sorts = _normalize_sorts(sorts)
    params = {
        'q': q,
        'sort[]': sorts,  # broken for encoding of + -> %2B
        'fl[]': RETURN_FIELDS,
        'rows': min(limit, MAX_IA_RESULTS) or MAX_IA_RESULTS,
        'page': page,
        'output': 'json'
    }
    url = SEARCH_URL + '?' + urllib.urlencode(params, doseq=True)
    items = _request_items(url)
    work2item = _index_item_by_distinct_work(items)
    editions = [
        _add_availability_to_edition(
            edition.canonicalize, work2item).dict()
        for edition in web.ctx.site.get_many([
            '/books/%s' % item['openlibrary_edition']
            for item in work2item.values()
        ]) if _item_matching_edition(edition, work2item)
    ]

    return {
        'editions': editions,
        'advancedsearch_url': url,
        'browse_url': _prepare_browsable_query(q, sorts),
        'params': params
    }

def _normalize_sorts(sorts):
    """_request requires sorts to be a list of valid sort options.
    Discard invalid sorts and marshal to list.
    """
    VALID_SORTS = [
        '__random', '__sort', 'addeddate', 'avg_rating', 'call_number',
        'createdate', 'creatorSorter', 'creatorSorterRaw', 'date',
        'downloads', 'foldoutcount', 'headerImage', 'identifier',
        'identifierSorter', 'imagecount', 'indexdate',
        'item_size', 'languageSorter', 'licenseurl', 'mediatype',
        'mediatypeSorter', 'month', 'nav_order', 'num_reviews',
        'programSorter', 'publicdate', 'reviewdate', 'stars',
        'titleSorter', 'titleSorterRaw', 'week',  'year',
        'loans__status__last_loan_date'
    ]
    if sorts:
        # If it's a string, split and turn to list
        if isinstance(sorts, six.string_types):
            sorts = sorts.split(',')
        if isinstance(sorts, list):
            # compare against field with +asc/desc suffix rm'd
            # e.g: date, not date+asc or date+desc
            return [sort.replace('+', ' ') for sort in sorts
                    if sort.split('+')[0] in VALID_SORTS]
    return ['']

def _prepare_api_query(query):
    """Expands short / more easily cacheable preset queries
    and fixes query to ensure only text archive.org items
    are returned having openlibrary_work
    """
    # Expand preset queries
    if query in PRESET_QUERIES:
        query = PRESET_QUERIES[query]

    q = 'mediatype:texts AND !noindex:* AND openlibrary_work:(*)'
    # Add availability if none present (e.g. borrowable only)
    if AVAILABILITY_STATUS not in query:
        q += ' AND %s:AVAILABLE' % AVAILABILITY_STATUS
    return q if not query else q + " AND " + query

def _prepare_browsable_query(query, sorts):
    """If the client wants to explore this query in the browser, it will
    have to be converted to user a human readable version of the
    advancedsearch API
    """
    _sort = sorts[0] if sorts else ''
    if ' desc' in _sort:
        _sort = '-' + _sort.split(' ')[0]
    elif ' asc' in _sort:
        _sort = _sort.split(' ')[0]
    return '%s?query=%s&sort=%s' % (BROWSE_URL, query, _sort)

def _request_items(url):
    """Hits archive.org advancedsearch.php API, returns matching items
    """
    try:
        request = urllib2.Request(url)
        # Internet Archive Elastic Search (which powers some of our
        # carousel queries) needs Open Library to forward user IPs so
        # we can attribute requests to end-users
        client_ip = web.ctx.env.get('HTTP_X_FORWARDED_FOR', 'ol-internal')
        request.add_header('x-client-id', client_ip)

        response = urllib2.urlopen(
            request, timeout=config_http_request_timeout).read()
        return simplejson.loads(
            response).get('response', {}).get('docs', [])
    except Exception as e:
        return []

def _index_item_by_distinct_work(items):
    """Filter duplicate editions (items with the same work)
    to ensure a single edition (item) per work
    """
    return dict(('/works/%s' % item['openlibrary_work'], item)
                for item in items)

def _item_matching_edition(edition, work2item):
    """An edition may belong to multiple works, especially if those works
    were  merged duplicates. This method tells us which work is listed in the
    work2item mapping.
    """
    return edition.works and next((
        work2item.get(work.key) for work in edition.works if work.key in work2item
    ), None)

def _add_availability_to_edition(edition, work2item):
    """
    To avoid a 2nd network call to `lending.add_availability`
    reconstruct availability ad-hoc from archive.org
    advancedsearch results

    XXX needs to be more robust if not item
    """
    item = _item_matching_edition(edition, work2item)
    availability_status = (
        'borrow_%s' % item[AVAILABILITY_STATUS].lower()
        if item.get(AVAILABILITY_STATUS) else 'open')
    edition['availability'] = {
        'status': availability_status,
        'identifier': item['identifier'],
        'openlibrary_edition': item['openlibrary_edition'],
        'openlibrary_work': item['openlibrary_work']
    }
    return edition