import os
import time
import fnmatch
import logging
import json
from flask import Flask, Response, request, redirect, jsonify, render_template, make_response
from repository import Repository, MercurialException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.realpath(__file__))
TRANSPLANT_FILTER = os.path.join(PROJECT_DIR, 'transplant_filter.py')
PULL_INTERVAL = 60
MAX_COMMITS = 100

Repository.register_extension('collapse', os.path.join(PROJECT_DIR, 'vendor', 'hgext', 'collapse.py'))

app = Flask(__name__)
app.config.from_object('config')

# make sure that WORKDIR exists
if not os.path.exists(app.config['WORKDIR']):
    os.makedirs(app.config['WORKDIR'])



def is_allowed_transplant(src, dst):
    return src != dst

def find_repo(name):
    for repository in app.config['REPOSITORIES']:
        if repository['name'] == name:
            return repository

    return None

def has_repo(name):
    repository = find_repo(name)
    if repository is None:
        return False

    return True

def get_repo_url(name):
    repository = find_repo(name)
    if repository is None:
        return None

    return repository['path']

def get_repo_dir(name):
    return os.path.abspath(os.path.join(app.config['WORKDIR'], name))

def clone_or_pull(name, refresh=False):
    repo_url = get_repo_url(name)
    repo_dir = get_repo_dir(name)

    if not os.path.exists(repo_dir):
        logger.info('cloning repository "%s"', name)
        repository = Repository.clone(repo_url, repo_dir)
        set_last_pull_date(name)
    else:
        repository = Repository(repo_dir)

        last_pull_date = get_last_pull_date(name)

        if refresh or last_pull_date < time.time() - PULL_INTERVAL:
            logger.info('pulling repository "%s"', name)
            repository.pull(update=True)
            set_last_pull_date(name)

    return repository

def get_last_pull_date(name):
    repo_dir = get_repo_dir(name)
    last_pull_date_file = os.path.join(repo_dir, '.hg', 'last_pull_date')
    if not os.path.exists(last_pull_date_file):
        return 0.0

    with open(last_pull_date_file, 'r') as f:
        try:
            timestamp = float(f.read())
        except Exception, e:
            logger.exception('could not read last pull date')
            return 0.0

    return timestamp

def set_last_pull_date(name):
    timestamp = time.time()
    repo_dir = get_repo_dir(name)
    last_pull_date_file = os.path.join(repo_dir, '.hg', 'last_pull_date')
    with open(last_pull_date_file, 'w') as f:
        f.write(str(timestamp))

def get_commit_info(repository, commit_id):
    try:
        log = repository.log(rev=commit_id)
        return log[0]
    except MercurialException, e:
        if 'unknown revision' in e.stderr:
            return False

def get_revsets_info(repository, revsets):
    return map(lambda revset: get_revset_info(repository, revset), revsets)

def get_revset_info(repository, revset):
    commits = repository.log(rev=revset)
    commits_count = len(commits)
    if commits_count > MAX_COMMITS:
        msg = too_many_commits_error(commits_count, MAX_COMMITS)
        raise TooManyCommitsError(msg)

    return {
        "revset": revset,
        "commits": commits
    }

def cleanup(repo):
    logger.info('cleaning up')
    repo.update(clean=True)
    repo.purge(abort_on_err=True, all=True)

    try:
        repo.strip('outgoing()', no_backup=True)
    except MercurialException, e:
        if 'empty revision set' not in e.stderr:
            raise e

def raw_transplant(repository, source, revset, message=None):
    filter = None
    env = os.environ.copy()

    if message is not None:
        filter = TRANSPLANT_FILTER
        env['TRANSPLANT_MESSAGE'] = message

    return repository.transplant(revset, source=source, filter=filter, env=env)

def transplant(src, dst, items):
    try:
        clone_or_pull(src, refresh=True)
        dst_repo = clone_or_pull(dst, refresh=True)

        try:
            for item in items:
                transplant_item(src, dst, item)

            logger.info('pushing "%s"', dst)
            dst_repo.push()

            tip = dst_repo.id(id=True)
            logger.info('tip: %s', tip)
            return jsonify({'tip': tip})

        finally:
            #cleanup(dst_repo)
            pass

    except MercurialException, e:
        print e
        return jsonify({
            'error': 'Transplant failed',
            'details': {
                'cmd': e.cmd,
                'returncode': e.returncode,
                'stdout': e.stdout,
                'stderr': e.stderr
            }
        }), 409

def transplant_item(src, dst, item):
    if 'commit' in item:
        transplant_commit(src, dst, item)
    elif 'revset' in item:
        transplant_revset(src, dst, item)
    else:
        raise Exception("unknown item: {}".format(item))

def transplant_commit(src, dst, item):
    message = item.get('message', None)
    _transplant(src, dst, item['commit'], message=message)

def transplant_revset(src, dst, item):
    message = item.get('message', None)

    src_repo = Repository(get_repo_dir(src))
    dst_repo = Repository(get_repo_dir(dst))
    commits = src_repo.log(rev=item['revset'])
    commits_count = len(commits)
    if commits_count > MAX_COMMITS:
        msg = too_many_commits_error(commits_count, MAX_COMMITS)
        raise TooManyCommitsError(msg)

    if commits_count == 0:
        return

    if commits_count == 1:
      _transplant(src, dst, item['revset'], message=message)
    else:
      old_tip = dst_repo.id(id=True)
      revset = [commit['node'] for commit in commits]

      # no need to pass message as we'll override it during collapse anyway
      _transplant(src, dst, revset)

      collapse_rev = 'descendants(children({}))'.format(old_tip)
      collapse_commits = dst_repo.log(rev=collapse_rev)

      # less than two commits were transplanted, no need to squash
      if len(collapse_commits) < 2:
        return

      logger.info('collapsing "%s"', collapse_rev)
      dst_repo.collapse(rev=collapse_rev, message=message)


def _transplant(src, dst, revset, message=None):
    dst_repo = Repository(get_repo_dir(dst))
    src_dir = get_repo_dir(src)

    logger.info('transplanting "%s" from "%s" to "%s"', revset, src, dst)

    result = raw_transplant(dst_repo, src_dir, revset, message=message)
    dst_repo.update()

    logger.debug('hg transplant: %s', result)


def too_many_commits_error(current, limit):
    return "You're trying to transplant {} commits which is above {} commits limit".format(current, limit)

@app.route('/')
def flask_index():
    return app.send_static_file('index.html')

@app.route('/config.js')
def flask_config_js():
    config_js = render_template('config.js.j2', repositories=app.config['REPOSITORIES'])
    response = make_response(config_js)
    response.headers["Content-Type"] = "application/javascript"
    return response

@app.route('/repositories/<repository_id>/revsets')
def flask_get_revsets(repository_id):
    revsets = request.values.get('revsets')
    if not revsets:
        return jsonify({'error': 'No revsets'}), 400
    revsets = json.loads(revsets)

    repository = clone_or_pull(repository_id)
    try:
        revsets_info = get_revsets_info(repository, revsets)
    except TooManyCommitsError, e:
        return jsonify({
            'error': e.message
        }), 400
    except MercurialException, e:
        return jsonify({
            'error': e.stderr
        }), 400

    return jsonify({
        'revsets': revsets_info
    })

@app.route('/transplant', methods = ['POST'])
def flask_transplant():
    params = request.get_json()
    if not params:
        return jsonify({'error': 'No params'}), 400

    src = params.get('src')
    dst = params.get('dst')
    items = params.get('items')

    if not src:
        return jsonify({'error': 'No src'}), 400

    if not dst:
        return jsonify({'error': 'No dst'}), 400

    if not items:
        return jsonify({'error': 'No items'}), 400

    if not has_repo(src):
        msg = 'Unknown src repository: {}'.format(src)
        return jsonify({'error': msg}), 400

    if not has_repo(dst):
        msg = 'Unknown dst repository: {}'.format(dst)
        return jsonify({'error': msg}), 400

    if not is_allowed_transplant(src, dst):
        msg = 'Transplant from {} to {} is not allowed'.format(src, dst)
        return jsonify({'error': msg}), 400

    return transplant(src, dst, items)

class TooManyCommitsError(Exception):
    pass

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
