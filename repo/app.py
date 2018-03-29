import logging
import os
from datetime import datetime, timedelta

import pandas as pd
import psycopg2
import schedule
from flask import Flask, g, jsonify, make_response, render_template, request
from flask_assets import Bundle, Environment
from psycopg2.extras import RealDictCursor

import distill
from repo.converter import rtf_to_text
from repo.database.connection import open_connection
from repo.database.contrast_medium import query_contrast_medium
from repo.database.report import query_report_by_befund_status
from repo.database.review_report import (query_review_report,
                                         query_review_reports)
from repo.nlp import classify
from repo.report import get_as_rtf, get_as_txt, get_with_file, q
from review.calculations import relative
from review.database import query_by_writer

app = Flask(__name__, instance_relative_config=True)
app.config.from_object('repo.default_config')
app.config.from_pyfile('config.cfg')
version = app.config['VERSION'] = '3.0.4'

RIS_DB_SETTINGS = {
    'host': app.config['RIS_DB_HOST'],
    'port': app.config['RIS_DB_PORT'],
    'service': app.config['RIS_DB_SERVICE'],
    'user': app.config['RIS_DB_USER'],
    'password': app.config['RIS_DB_PASSWORD']
}

REVIEW_DB_SETTINGS = {
    'dbname': app.config['REVIEW_DB_NAME'],
    'user': app.config['REVIEW_DB_USER'],
    'password': app.config['REVIEW_DB_PASSWORD'],
    'host': 'localhost'
}

REPORTS_FOLDER = 'reports'
if not os.path.exists(REPORTS_FOLDER):
    os.makedirs(REPORTS_FOLDER, exist_ok=True)

assets = Environment(app)
js = Bundle("js/jquery-3.1.0.min.js", "js/moment.min.js",
            "js/pikaday.js", "js/pikaday.jquery.js",
            "js/script.js",
            filters='jsmin', output='gen/packed.js')
assets.register('js_all', js)


@app.route('/')
def main():
    return render_template('index.html', version=app.config['VERSION'])


@app.route('/q')
def query():
    day = request.args.get('day', '')
    dd = datetime.strptime(day, '%Y-%m-%d')
    parse_text = request.args.get('parse', False)
    if not day:
        logging.debug('No day given, returning to main view')
        return main()
    con = get_ris_db()
    rows = q(con.cursor(), dd, parse_text)
    return jsonify(rows)


@app.route('/review')
def review():
    now = datetime.now().strftime('%d.%m.%Y')
    day = request.args.get('day', now)
    writer = request.args.get('writer', '')
    dd = datetime.strptime(day, '%d.%m.%Y')
    con =  get_review_db()
    rows = query_review_reports(con.cursor(), dd, writer)
    day = dd.strftime('%d.%m.%Y')
    return render_template('review.html',
        rows=rows, day=day, writer=writer, version=version)


@app.route('/review/diff/<id>')
def diff(id):
    con =  get_review_db()
    row = query_review_report(con.cursor(), id)
    cases = ['befund_s', 'befund_g', 'befund_f']
    for c in cases:
        if c in row:
            field = c + '_text'
            v = row[c]
            if v:
                row[field] = rtf_to_text(v)
    return render_template('diff.html', row=row, version=version)


@app.route('/review/dashboard')
def dashboard():
    writer = request.args.get('w', '')
    last_exams = int(request.args.get('last_exams', '30'))
    con = get_review_db()
    cursor = con.cursor(cursor_factory=RealDictCursor)
    rows = query_by_writer(cursor, writer, last_exams)
    return render_template('dashboard.html',
        rows=rows, writer=writer, last_exams=last_exams, version=version)


@app.route('/review/dashboard/data/<writer>/<last_exams>')
def data(writer, last_exams):
    con = get_review_db()
    cursor = con.cursor(cursor_factory=RealDictCursor)
    rows = query_by_writer(cursor, writer, last_exams)
    if len(rows) > 0:
        df = pd.DataFrame(rows)
        df = relative(df).sort_values('unters_beginn')
        return df.to_csv(index_label='index')
    return pd.DataFrame().to_csv(index_label='index')

@app.route('/cm')
def cm():
    "Queries for contrast medium for a accession number"
    accession_number = request.args.get('accession_number', '')
    if not accession_number:
        print('No accession number found in request, use accession_number=XXX')
        return main()
    con = get_ris_db()
    result = query_contrast_medium(con.cursor(), accession_number)
    return jsonify(result)


@app.route('/show')
def show():
    """ Renders RIS Report as HTML. """
    accession_number = request.args.get('accession_number', '')
    output = request.args.get('output', 'html')
    # if no accession number is given -> render main page
    if not accession_number:
        print('No accession number found in request, use accession_number=XXX')
        return main()

    con = get_ris_db()
    if output == 'text':
        report_as_text, meta_data = get_as_txt(con.cursor(), accession_number)
        if report_as_text:
            return report_as_text
        else:
            # don't throw an error, no report found -> return empty response
            # because not all accession numbers have a valid report
            return ""
    else:
        report_as_html, meta_data = get_with_file(con.cursor(), accession_number)
        return render_template('report.html',
                               version=app.config['VERSION'],
                               accession_number=accession_number,
                               meta_data=meta_data,
                               report=report_as_html)


@app.route('/nlp')
def nlp():
    """ Renders RIS Report as HTML. """
    accession_number = request.args.get('accession_number', '')
    output = request.args.get('output', 'html')
    # if no accession number is given -> render main page
    if not accession_number:
        print('No accession number found in request, use accession_number=XXX')
        return main()

    con = get_ris_db()
    report_as_text, meta_data = get_as_txt(con.cursor(), accession_number)
    report_as_html, meta_data = get_with_file(con.cursor(), accession_number)
    result = distill.process(report_as_text, meta_data)
    #clas = {'nlp': classify(report_as_text)}
    #z = {**result, **clas}

    output = request.args.get('output', 'html')
    if output == 'json':
        j = {}
        j['report'] = report_as_text
        j['meta_data'] = meta_data
        j['distill'] = result
        return jsonify(j)
    else:
        return render_template('nlp.html',
                                version=app.config['VERSION'],
                                accession_number=accession_number,
                                meta_data=meta_data,
                                nlp=result,
                                report=report_as_html)


@app.route('/download')
def download():
    """ Downloads the original RTF report. """
    accession_number = request.args.get('accession_number', '')
    if not accession_number:
        return ""
    con = get_ris_db()
    report = get_as_rtf(con.cursor(), accession_number)
    response = make_response(report)
    cd = 'attachment; filename={}.rtf'.format(accession_number)
    response.headers['Content-Disposition'] = cd
    return response


def get_review_db():
    "Returns a connection to the PostgreSQL Review DB"
    db = getattr(g, '_review_database', None)
    if db is None:
        db = g._review_database = psycopg2.connect(**REVIEW_DB_SETTINGS)
    return g._review_database


def get_ris_db():
    """ Returns a connection to the Oracle db. """
    db = getattr(g, '_ris_database', None)
    if db is None:
        db = g._ris_database = open_connection(**RIS_DB_SETTINGS)
    return g._ris_database


@app.teardown_appcontext
def teardown_db(exception):
    """ Closes DB connection when app context is done. """
    logging.debug('Closing db connection')
    db = getattr(g, '_ris_database', None)
    if db is not None:
        db.close()
