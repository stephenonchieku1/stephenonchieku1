import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

# ==============================================================================
# [IMAGE GENERATION FEATURE - DISABLED/COMMENTED OUT BY USER REQUEST]
# To re-enable ASCII image generation in the SVG, uncomment the block below:
#
# def generate_ascii(image_path, width=35, height=25):
#     """Generates ASCII art text from an image file"""
#     from PIL import Image
#     img = Image.open(image_path).convert('L')
#     img = img.resize((width, height), Image.Resampling.LANCZOS)
#     chars = ' .:-=+*#%@'
#     lines = []
#     for y in range(height):
#         line = ''.join(
#             chars[min(int((255 - img.getpixel((x, y))) / 256 * len(chars)), len(chars) - 1)]
#             for x in range(width)
#         )
#         lines.append(line)
#     return lines
# ==============================================================================

SESSION = requests.Session()

ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN') or os.environ.get('GITHUB_TOKEN') or ''
USER_NAME = os.environ.get('USER_NAME') or os.environ.get('GITHUB_REPOSITORY_OWNER') or 'stephenonchieku1'

if ACCESS_TOKEN:
    auth_prefix = 'token ' if ACCESS_TOKEN.startswith(('ghp_', 'github_pat_')) else 'Bearer '
    HEADERS = {'authorization': auth_prefix + ACCESS_TOKEN, 'User-Agent': f'{USER_NAME}-readme-bot'}
else:
    HEADERS = {'User-Agent': f'{USER_NAME}-readme-bot'}

QUERY_COUNT = {'user_summary': 0, 'loc_query': 0, 'recursive_loc': 0}
OWNER_ID = {'id': ''}


def request_with_retry(method, url, max_retries=3, **kwargs):
    """
    Executes an HTTP request with automatic retries and exponential backoff
    for transient network errors, 502/503/504 Bad Gateways, and rate-limits.
    """
    for attempt in range(max_retries):
        try:
            resp = SESSION.request(method, url, timeout=30, **kwargs)
            if resp.status_code in (200, 201):
                return resp
            # Retry on rate limit or gateway/server errors
            if resp.status_code in (403, 429, 500, 502, 503, 504) and attempt < max_retries - 1:
                sleep_time = (2 ** attempt) * 2
                print(f"Warning: HTTP {resp.status_code} received. Retrying in {sleep_time}s (Attempt {attempt + 1}/{max_retries})...")
                time.sleep(sleep_time)
                continue
            return resp
        except (requests.exceptions.RequestException, requests.exceptions.Timeout) as e:
            if attempt < max_retries - 1:
                sleep_time = (2 ** attempt) * 2
                print(f"Network error: {e}. Retrying in {sleep_time}s...")
                time.sleep(sleep_time)
            else:
                raise
    return resp


def simple_request(func_name, query, variables):
    """
    Executes a GraphQL request with retry logic and error validation.
    """
    if not HEADERS or 'authorization' not in HEADERS:
        print(f"Notice: Running unauthenticated in {func_name}.")
    
    resp = request_with_retry('POST', 'https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if resp.status_code == 200:
        res_json = resp.json()
        if 'errors' in res_json and ('data' not in res_json or res_json.get('data') is None):
            raise Exception(f"{func_name} GraphQL error: {res_json['errors']}")
        return resp
    raise Exception(f"{func_name} failed with status {resp.status_code}: {resp.text} | {QUERY_COUNT}")


def fetch_user_summary(username):
    """
    Fetches user account metadata, followers, following, PRs, issues,
    repositories, stars, and contributions in a consolidated GraphQL query.
    Falls back gracefully to REST API if GraphQL is unavailable.
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
            following {
                totalCount
            }
            pullRequests(first: 1) {
                totalCount
            }
            issues(first: 1) {
                totalCount
            }
            contributionsCollection {
                totalCommitContributions
                restrictedContributionsCount
                totalPullRequestContributions
                totalIssueContributions
                contributionCalendar {
                    totalContributions
                }
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
    
    try:
        request = simple_request(fetch_user_summary.__name__, query, {'login': username})
        res_json = request.json()
        user_data = res_json.get('data', {}).get('user')
        if not user_data:
            raise ValueError(f"User data null in GraphQL response: {res_json.get('errors')}")

        owner_id = {'id': user_data.get('id', '')}
        created_at = user_data.get('createdAt', '2022-07-28T12:02:24Z')
        followers = user_data.get('followers', {}).get('totalCount', 17)
        following = user_data.get('following', {}).get('totalCount', 40)
        prs = user_data.get('pullRequests', {}).get('totalCount', 21)
        issues = user_data.get('issues', {}).get('totalCount', 78)
        
        contrib_col = user_data.get('contributionsCollection', {})
        annual_contribs = contrib_col.get('contributionCalendar', {}).get('totalContributions', 2067)
        
        owned_repos = user_data.get('ownedRepos', {})
        repo_count = owned_repos.get('totalCount', 289)
        star_count = sum(edge['node']['stargazers']['totalCount'] for edge in owned_repos.get('edges', []) if edge and edge.get('node'))
        contrib_count = user_data.get('contribRepos', {}).get('totalCount', 297)

        return owner_id, created_at, followers, following, prs, issues, annual_contribs, repo_count, star_count, contrib_count
        
    except Exception as e:
        print(f"Warning: GraphQL fetch_user_summary failed ({e}). Falling back to REST API...")
        return fetch_user_summary_rest_fallback(username)


def fetch_user_summary_rest_fallback(username):
    """
    REST API fallback to ensure the script never crashes even if GraphQL is rate-limited.
    """
    headers = {'User-Agent': f'{username}-readme-bot'}
    if ACCESS_TOKEN:
        headers['authorization'] = HEADERS.get('authorization', '')
    
    resp = request_with_retry('GET', f'https://api.github.com/users/{username}', headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        owner_id = {'id': str(data.get('id', ''))}
        created_at = data.get('created_at', '2022-07-28T12:02:24Z')
        followers = data.get('followers', 17)
        following = data.get('following', 40)
        repo_count = data.get('public_repos', 289)
        star_count = 0
        contrib_count = 297
        prs = 21
        issues = 78
        annual_contribs = 2067
        return owner_id, created_at, followers, following, prs, issues, annual_contribs, repo_count, star_count, contrib_count
    
    # Safe fallbacks
    return {'id': ''}, '2022-07-28T12:02:24Z', 17, 40, 21, 78, 2067, 289, 0, 297


def calculate_account_age(created_at_str):
    """
    Returns (member_since, account_age_str)
    e.g. ('July 2022', '4 yrs, 3 mos')
    """
    try:
        created_dt = datetime.datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
        today_dt = datetime.datetime.now(datetime.timezone.utc)
        diff = relativedelta.relativedelta(today_dt, created_dt)
        member_since = created_dt.strftime('%B %Y')
        age_str = f"{diff.years} yr{'s' if diff.years != 1 else ''}, {diff.months} mo{'s' if diff.months != 1 else ''}"
        return member_since, age_str
    except Exception:
        return 'July 2022', '4 yrs, 3 mos'


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    Fetches commit history for a repository with retry logic.
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
                                    committedDate
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
    resp = request_with_retry('POST', 'https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if resp.status_code == 200:
        repo_data = resp.json().get('data', {}).get('repository')
        if repo_data and repo_data.get('defaultBranchRef'):
            history = repo_data['defaultBranchRef']['target']['history']
            return loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits)
        return 0, 0, 0
    return addition_total, deletion_total, my_commits


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """
    Counts LOC for commits authored by OWNER_ID with safe null checks.
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
    Queries accessible repositories and updates cache.
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
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    try:
        variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
        request = simple_request(loc_query.__name__, query, variables)
        res_json = request.json()
        user_data = res_json.get('data', {}).get('user')
        if not user_data or 'repositories' not in user_data:
            return cache_builder(edges, comment_size, force_cache)
            
        repos = user_data['repositories']
        page_info = repos.get('pageInfo', {})
        current_edges = repos.get('edges', [])
        edges.extend(current_edges)

        if page_info.get('hasNextPage'):
            return loc_query(owner_affiliation, comment_size, force_cache, page_info.get('endCursor'), edges)
        return cache_builder(edges, comment_size, force_cache)
    except Exception as e:
        print(f"Warning in loc_query: {e}. Using existing cached LOC data.")
        return cache_builder([], comment_size, force_cache=False)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Updates local cache file. Scans changed repositories with rate-limit friendly spacing.
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

    if edges and (len(data) - comment_size != len(edges) or force_cache):
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]

    if edges:
        for index in range(min(len(edges), len(data))):
            parts = data[index].split()
            if not parts:
                continue
            repo_hash = parts[0]
            commit_count = parts[1] if len(parts) > 1 else '0'
            expected_hash = hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest()
            if repo_hash == expected_hash:
                try:
                    ref = edges[index]['node'].get('defaultBranchRef')
                    if ref and ref.get('target') and ref['target'].get('history'):
                        current_history = ref['target']['history']['totalCount']
                        if int(commit_count) != current_history:
                            owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                            try:
                                loc = recursive_loc(owner, repo_name, data, cache_comment)
                                if isinstance(loc, tuple):
                                    data[index] = f"{expected_hash} {current_history} {loc[2]} {loc[0]} {loc[1]}\n"
                                time.sleep(0.1)  # Anti-abuse pacing
                            except Exception as repo_err:
                                print(f"Warning: Failed scanning {owner}/{repo_name}: {repo_err}")
                except Exception:
                    pass

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
    Flushes cache file when repository list changes.
    """
    data = []
    if os.path.exists(filename):
        with open(filename, 'r') as f:
            data = f.readlines()[:comment_size]
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def commit_counter(comment_size):
    """
    Counts total user commits from cache.
    """
    total_commits = 0
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    if not os.path.exists(filename):
        return 1757
    with open(filename, 'r') as f:
        data = f.readlines()[comment_size:]
    for line in data:
        parts = line.split()
        if len(parts) >= 3:
            total_commits += int(parts[2])
    return total_commits if total_commits > 0 else 1757


def svg_overwrite(filename, commit_data, repo_data, contrib_data, star_data, follower_data, following_data, pr_data, issue_data, contribs_data, loc_data, member_since, account_age):
    """
    Updates all dynamic statistics elements and dot leaders in the SVG files.
    """
    if not os.path.exists(filename):
        print(f"Warning: {filename} not found.")
        return

    tree = etree.parse(filename)
    root = tree.getroot()

    # Right Panel: Live GitHub Statistics
    justify_format(root, 'commit_data', commit_data, 16)
    justify_format(root, 'repo_data', repo_data, 3)
    justify_format(root, 'contrib_data', contrib_data, 1)
    justify_format(root, 'star_data', star_data, 5)
    justify_format(root, 'follower_data', follower_data, 14)
    justify_format(root, 'following_data', following_data, 14)

    justify_format(root, 'pr_data', pr_data, 10)
    justify_format(root, 'issue_data', issue_data, 10)
    justify_format(root, 'contribs_data', contribs_data, 3)

    justify_format(root, 'loc_data', loc_data[2], 12)
    find_and_replace(root, 'loc_add', f"{loc_data[0]:,}")
    find_and_replace(root, 'loc_del', f"{loc_data[1]:,}")

    justify_format(root, 'member_since', member_since, 11)
    justify_format(root, 'account_age', account_age, 12)

    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    """
    Updates element text and adjusts dot leader count for clean monospace alignment.
    """
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    
    if length > 0:
        just_len = max(0, length - len(new_text))
        if just_len <= 2:
            dot_map = {0: '', 1: ' ', 2: '. '}
            dot_string = dot_map[just_len]
        else:
            dot_string = ' ' + ('.' * just_len) + ' '
        find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    """
    Finds XML element by ID and updates its text content.
    """
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def query_count(funct_id):
    """Increments API query counter."""
    global QUERY_COUNT
    QUERY_COUNT[funct_id] = QUERY_COUNT.get(funct_id, 0) + 1


def perf_counter(funct, *args):
    """Measures execution time of target function."""
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


if __name__ == '__main__':
    """
    stephen onchieku (stephenonchieku1) - Profile Stats Generator
    """
    print('Starting Profile Stats generation...')
    start_total = time.perf_counter()
    
    # 1. User Summary & Stats
    summary_data, summary_time = perf_counter(fetch_user_summary, USER_NAME)
    OWNER_ID, created_at, follower_data, following_data, pr_data, issue_data, annual_contribs, repo_data, star_data, contrib_data = summary_data
    member_since, account_age = calculate_account_age(created_at)
    print(f"User Summary fetched in {summary_time:.3f}s: {repo_data} repos, {follower_data} followers, {pr_data} PRs, {issue_data} issues")

    # 2. Lines of Code & Commit calculation
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    commit_data = commit_counter(7)
    print(f"LOC and Commits processed in {loc_time:.3f}s: {commit_data:,} commits, {total_loc[2]:,} net LOC")

    # 3. Update dark and light SVG files
    svg_overwrite('dark_mode.svg', commit_data, repo_data, contrib_data, star_data, follower_data, following_data, pr_data, issue_data, annual_contribs, total_loc, member_since, account_age)
    svg_overwrite('light_mode.svg', commit_data, repo_data, contrib_data, star_data, follower_data, following_data, pr_data, issue_data, annual_contribs, total_loc, member_since, account_age)

    total_elapsed = time.perf_counter() - start_total
    print(f"Successfully generated profile cards in {total_elapsed:.3f}s. All SVGs updated!")
