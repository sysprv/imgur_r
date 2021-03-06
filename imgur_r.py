#! python

#    imgur_r.py -- Download images from imgur albums that correspond to
#    subreddits.
#    Copyright (C) 2012 oshadi@tinspoon.net
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

import http.client
import json
import sqlite3
import os
import os.path
import time
import logging
import re
import sys
import io
from contextlib import closing


class RegExps:
    reddit_r_name = re.compile(r'^/r/[A-Za-z0-9_-]+$')
    imgur_image_uri_path = re.compile(r'^/[A-Za-z0-9]+\.(jpg|gif|png)$')
    imgur_page_uri_path = re.compile(r'^/r/[A-Za-z0-9_-]+/page/[0-9]+\.json$')
    imgur_image_created_time = re.compile(r'^[0-9]+$')


def init_db(r):
    assert RegExps.reddit_r_name.match(r)

    filename = 'imgur' + r.replace('/', '_') + '.sqlite3'
    if not os.path.exists(filename):
        logging.info("Creating SQLite database " + filename)
        conn = create_db(filename)
    else:
        conn = sqlite3.connect(filename)

    return conn


def create_db(fn):
    conn = sqlite3.connect(fn)
    cur = conn.cursor()
    mk_tbl = 'CREATE TABLE imgur_r(\
hash TEXT PRIMARY KEY,\
title TEXT,\
complete_uri TEXT,\
final_filename TEXT,\
etag TEXT,\
datetime TEXT,\
mimetype TEXT,\
ext TEXT,\
width INTEGER,\
height INTEGER,\
size INTEGER,\
ups INTEGER,\
downs INTEGER,\
points INTEGER,\
permalink TEXT,\
subreddit TEXT,\
nsfw TEXT,\
created TEXT,\
score TEXT,\
author TEXT)'

    mk_idx = 'CREATE UNIQUE INDEX hash_nsfw \
ON imgur_r(hash, nsfw)'

    cur.execute(mk_tbl)
    cur.execute(mk_idx)

    conn.commit()
    cur.close()
    return conn


def dump_headers(resp):
    m = io.StringIO()
    m.write('headers ->\n')
    for tpl in resp.getheaders():
        m.write(tpl[0])
        m.write(' -> ')
        m.write(tpl[1])
        m.write('\n')
    m.write('<- headers\n\n')

    sys.stderr.write(m.getvalue())


def write_file(filename, data, mtime):
    tmp_filename = filename + '.part'
    if not os.path.exists(filename):
        f = open(tmp_filename, 'wb')
        f.write(data)
        f.close()
        # logging.info("Setting mtime = " + str(mtime) + ", " + time.asctime(time.gmtime(mtime)))
        os.utime(tmp_filename, (time.mktime(time.gmtime()), mtime))
        os.rename(tmp_filename, filename)


def already_downloaded(conn_db, hash):
    qry = 'SELECT hash FROM imgur_r WHERE hash = ?'
    hash_tpl = (hash,)
    cur = conn_db.cursor()
    rc = 0
    for row in cur.execute(qry, hash_tpl):
        rc += 1
    cur.close()
    return rc


# table fields:
# hash, title, complete_uri, final_filename, etag, datetime,
# mimetype, ext, width, height, size, ups, downs, points,
# permalink, subreddit, nsfw, created, score, author

def insert(conn_db, img, complete_uri, final_filename, etag):
    qry = 'INSERT INTO imgur_r VALUES(\
?,?,\
?,?,?,\
?,?,?,\
?,?,?,\
?,?,?,\
?,?,?,\
?,?,?)'

    values_tpl = (img['hash'], img['title'],
                  complete_uri, final_filename, etag,
                  img['datetime'], img['mimetype'], img['ext'],
                  img['width'], img['height'], img['size'],
                  img['ups'], img['downs'], img['points'],
                  img['permalink'], img['subreddit'], img['nsfw'],
                  img['created'], img['score'], img['author'])

    cur = conn_db.cursor()
    cur.execute(qry, values_tpl)
    conn_db.commit()
    cur.close()


def image_time(img):
    tm = -1

    if img['created']:
        if RegExps.imgur_image_created_time.match(str(img['created'])):
            tm = float(img['created'])

    if tm < 0:
        tm = time.mktime(time.gmtime())

    return tm


def handle_image(conn_db, conn_i, img):
    has = already_downloaded(conn_db, img['hash'])
    if has > 0:
        logging.info('Image with hash ' + img['hash'] + ' has already been downloaded')
        return 1

    direct_uri_path = '/' + img['hash'] + img['ext']
    assert RegExps.imgur_image_uri_path.match(direct_uri_path)
    direct_uri = 'http://i.imgur.com' + direct_uri_path
    filename = img['hash'] + img['ext'] # ext includes '.'

    logging.info('Downloading ' + direct_uri)

    conn_i.request('GET', direct_uri_path)
    resp = conn_i.getresponse()
    # dump_headers(resp)
    img_bin = resp.read()
    write_file(filename, img_bin, image_time(img))
    insert(conn_db, img, direct_uri, filename, resp.getheader('ETag'))
    return 0


def handle_page(conn_db, pg):
    complete = None
    for attempt in range(1, 4):
        # try up to 3 times
        if attempt > 1:
            logging.warning('Attempt number ' + str(attempt))
            time.sleep(5)

        try:
            with closing(http.client.HTTPConnection('i.imgur.com')) as conn_i:
                # http connection allocated here, so it can be reused for many images.
                for img in pg['gallery']:
                    # print(img['title'], img['permalink'], img['hash'], img['ext'])
                    handle_image(conn_db, conn_i, img)
        except http.client.BadStatusLine as bsl:
            # server closed connection
            complete = False
            logging.exception('Error while downloading images')
        else:
            # no error, all images in page downloaded
            complete = True

        if complete:
            break

    if not complete:
        raise IOError('Repeatedly failed to download images')


def get_imgur_page_json(r, pageno):
    with closing(http.client.HTTPConnection('imgur.com')) as conn:
        path = r + '/page/' + str(pageno) + '.json'
        assert RegExps.imgur_page_uri_path.match(path)

        logging.info('Fetching ' + path)
        conn.request('GET', path)
        resp = conn.getresponse()
        # dump_headers(resp)
        if resp.status == 404:
            raise StopIteration('No more pages to get')

        data = resp.read()

    data_s = str(data, encoding='utf8')
    # print(data_s)
    obj = json.loads(data_s)
    if len(obj['gallery']) == 0:
        logging.info('Found empty gallery page')
        raise StopIteration('Reached empty gallery')

    return obj


def imgur_r(r, starting_page = 0):
    # save subreddit name to be read later
    with closing(open('r', 'w')) as rf:
        rf.write(r)
        rf.write('\n')

    with closing(init_db(r)) as conn_db:
        pageno = starting_page
        while True:
            try:
                pg = get_imgur_page_json(r, pageno)
                handle_page(conn_db, pg)
                pageno += 1
                time.sleep(1.3)
            except StopIteration:
                break

    logging.info('All done')


def saved_subreddit_name():
    if os.path.exists('r'):
        with closing(open('r', 'r')) as rf:
            r = rf.readline().strip()

        if RegExps.reddit_r_name.match(r):
            return r
        else:
            sys.stderr.write('Bad subreddit name ' + r + '\n')
            return None
    else:
        sys.stderr.write('"r" not found\n')
        return None


if __name__ == '__main__':
    logging.basicConfig(format='%(asctime)s %(message)s', level=logging.DEBUG)
    if len(sys.argv) == 1:
        # if we've downloaded in this dir before,
        # and have saved the subreddit name in a file,
        # use that.
        r = saved_subreddit_name()
        if r != None:
            imgur_r(r)
    elif len(sys.argv) == 2:
        imgur_r(sys.argv[1])
    elif len(sys.argv) == 3:
        imgur_r(sys.argv[1], int(sys.argv[2]))
