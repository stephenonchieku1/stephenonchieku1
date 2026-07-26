import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor

# Initialize persistent HTTP session for connection pooling and speed
SESSION = requests.Session()

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN') or os.environ.get('GITHUB_TOKEN') or ''
USER_NAME = os.environ.get('USER_NAME') or os.environ.get('GITHUB_REPOSITORY_OWNER') or 'stephenonchieku1'

if ACCESS_TOKEN:
    auth_prefix = 'token ' if ACCESS_TOKEN.startswith(('ghp_', 'github_pat_')) else 'Bearer '
    HEADERS = {'authorization': auth_prefix + ACCESS_TOKEN, 'User-Agent': f'{USER_NAME}-readme-bot'}
else:
    HEADERS = {}
QUERY_COUNT = {'user_summary': 0, 'graph_commits': 0, 'loc_query': 0, 'recursive_loc': 0}
OWNER_ID = {'id': ''}


def daily_readme(birthday):
    """
    Returns the length of time since birthday using timezone-aware UTC date
    e.g. 'XX years, XX months, XX days'
    """
    today = datetime.datetime.now(datetime.timezone.utc).date()
    birth_date = birthday.date() if isinstance(birthday, datetime.datetime) else birthday
    diff = relativedelta.relativedelta(today, birth_date)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """
    Returns 's' for plural numbers, empty string for 1
    """
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    """
    Returns a request using connection pooling, or raises an Exception if response fails.
    """
    request = SESSION.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        return request
    raise Exception(f"{func_name} failed with status {request.status_code}: {request.text} | {QUERY_COUNT}")


def fetch_user_summary(username):
    """
    Consolidates user metadata, followers, owned repositories, stars, and contributed repository counts
    into a single GraphQL request to maximize speed and efficiency.
    """
    query_count('user_summary')
    query = '''
    query($login: String!) {
        user(login: $login) {
            id
            createdAt
            followers {
                totalCount
            }
            ownedRepos: repositories(first: 100, ownerAffiliations: [OWNER]) {
                totalCount
                edges {
                    node {
                        stargazers {
                            totalCount
                        }
                    }
                }
            }
            contribRepos: repositories(first: 1, ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
                totalCount
            }
        }
    }'''
    request = simple_request(fetch_user_summary.__name__, query, {'login': username})
    data = request.json()['data']['user']
    
    owner_id = {'id': data['id']}
    created_at = data['createdAt']
    followers = data['followers']['totalCount']
    repo_count = data['ownedRepos']['totalCount']
    star_count = sum(edge['node']['stargazers']['totalCount'] for edge in data['ownedRepos']['edges'])
    contrib_count = data['contribRepos']['totalCount']
    
    return owner_id, created_at, followers, repo_count, star_count, contrib_count


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return total commit count for a date range
    """
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date, 'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to fetch commits from a repository
    """
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = SESSION.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        repo_data = request.json().get('data', {}).get('repository')
        if repo_data and repo_data.get('defaultBranchRef'):
            history = repo_data['defaultBranchRef']['target']['history']
            return loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits)
        return 0, 0, 0
    force_close_file(data, cache_comment)
    if request.status_code == 403:
        raise Exception('Too many requests! Anti-abuse limit hit.')
    raise Exception(f'recursive_loc() failed with status {request.status_code}: {request.text} | {QUERY_COUNT}')


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """
    Counts LOC for commits authored by OWNER_ID with safe null checks
    """
    for node in history.get('edges', []):
        commit_node = node.get('node', {})
        author = commit_node.get('author')
        user = author.get('user') if author else None
        if user and user.get('id') == OWNER_ID.get('id'):
            my_commits += 1
            addition_total += commit_node.get('additions', 0)
            deletion_total += commit_node.get('deletions', 0)

    if not history.get('edges') or not history.get('pageInfo', {}).get('hasNextPage'):
        return addition_total, deletion_total, my_commits
    return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    """
    Uses GitHub's GraphQL v4 API to query repositories accessible to user.
    """
    if edges is None:
        edges = []
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history {
                                            totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    page_info = request.json()['data']['user']['repositories']['pageInfo']
    current_edges = request.json()['data']['user']['repositories']['edges']
    edges.extend(current_edges)

    if page_info['hasNextPage']:
        return loc_query(owner_affiliation, comment_size, force_cache, page_info['endCursor'], edges)
    return cache_builder(edges, comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Checks repositories against local cache file, updating modified repositories in parallel using ThreadPoolExecutor.
    """
    os.makedirs('cache', exist_ok=True)
    cached = True
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]

    tasks = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        for index in range(len(edges)):
            repo_hash, commit_count, *__ = data[index].split()
            expected_hash = hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest()
            if repo_hash == expected_hash:
                try:
                    current_history = edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']
                    if int(commit_count) != current_history:
                        owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                        future = executor.submit(recursive_loc, owner, repo_name, data, cache_comment)
                        tasks.append((index, expected_hash, current_history, future))
                except (TypeError, KeyError):
                    data[index] = expected_hash + ' 0 0 0 0\n'

        for index, expected_hash, current_history, future in tasks:
            loc = future.result()
            if isinstance(loc, tuple):
                data[index] = f"{expected_hash} {current_history} {loc[2]} {loc[0]} {loc[1]}\n"

    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)

    for line in data:
        loc = line.split()
        if len(loc) >= 5:
            loc_add += int(loc[3])
            loc_del += int(loc[4])

    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """
    Flushes cache file when repository list changes
    """
    data = []
    if os.path.exists(filename):
        with open(filename, 'r') as f:
            data = f.readlines()[:comment_size]
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def force_close_file(data, cache_comment):
    """
    Saves cache state if program encounters an error
    """
    os.makedirs('cache', exist_ok=True)
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('Error occurred while fetching LOC data; saved partial cache state to:', filename)


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """
    Parse SVG files and update elements with age, commits, stars, repositories, and LOC data
    """
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'commit_data', commit_data, 8)
    justify_format(root, 'star_data', star_data, 6)
    justify_format(root, 'repo_data', repo_data, 6)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'follower_data', follower_data, 5)
    justify_format(root, 'loc_data', loc_data[2], 9)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1], 7)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    """
    Updates and formats SVG element text and calculates dot leader justification
    """
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    """
    Replaces target element text in XML tree
    """
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    """
    Counts total commits using local cache file
    """
    total_commits = 0
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    if not os.path.exists(filename):
        return 0
    with open(filename, 'r') as f:
        data = f.readlines()[comment_size:]
    for line in data:
        parts = line.split()
        if len(parts) >= 3:
            total_commits += int(parts[2])
    return total_commits


def query_count(funct_id):
    """
    Increments GraphQL API call counter
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] = QUERY_COUNT.get(funct_id, 0) + 1


def perf_counter(funct, *args):
    """
    Measures execution time of target function
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Prints formatted performance summary line
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    if difference > 1:
        print('{:>12}'.format('%.4f' % difference + ' s '))
    else:
        print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    """
    stephen onchieku (stephenonchieku1)
    """
    print('Calculation times:')
    
    # 1. Consolidated GraphQL call for user metadata, stars, repos, followers
    summary_data, summary_time = perf_counter(fetch_user_summary, USER_NAME)
    OWNER_ID, acc_date, follower_data, repo_data, star_data, contrib_data = summary_data
    formatter('user summary data', summary_time)
    
    # 2. Timezone-aware age calculation
    age_data, age_time = perf_counter(daily_readme, datetime.datetime(2002, 11, 10))
    formatter('age calculation', age_time)
    
    # 3. Lines of Code calculation with parallel repo fetching
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)
    
    # 4. Commit calculation from cache
    commit_data = commit_counter(7)
    commit_time = 0.0001
    
    for index in range(len(total_loc) - 1):
        total_loc[index] = '{:,}'.format(total_loc[index])

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])

    print(f"Total function time: {(summary_time + age_time + loc_time):.4f} s")
    print('Total GitHub GraphQL API calls:', sum(QUERY_COUNT.values()))
    for funct_name, count in QUERY_COUNT.items():
        print(f"   {funct_name}: {count}")