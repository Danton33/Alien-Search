"""robots.txt checks (RFC 9309) for live government sites.

Python 3.13's urllib.robotparser implements longest-match and wildcard rules; Python 3.10-3.12 do
not. Where the standard library is incomplete this module is deliberately cautious: any matching
Disallow rule blocks the URL and Allow lines are ignored. Blocking a little too much is the safe error.
"""
import re
import string
import urllib.parse
import urllib.robotparser

TOKEN = 'AlienSearch'  # RFC 9309 product token: letters, underscores and hyphens only


def _stdlib_follows_rfc9309():
    rp=urllib.robotparser.RobotFileParser()
    rp.parse(['User-agent: *','Disallow: /a/','Allow: /a/b/','Disallow: /*.zip$'])
    return rp.can_fetch(TOKEN,'https://h/a/b/c') and not rp.can_fetch(TOKEN,'https://h/x/y.zip')


STDLIB_OK = _stdlib_follows_rfc9309()


def _groups(lines):
    groups=[]; agents=[]; rules=[]; last_was_agent=False
    for raw in lines:
        line=raw.split('#',1)[0].strip()
        if ':' not in line: continue
        field,value=(part.strip() for part in line.split(':',1))
        field=field.lower()
        if field=='user-agent':
            if not last_was_agent and (agents or rules):
                groups.append((agents,rules)); agents=[]; rules=[]
            agents.append(value.lower()); last_was_agent=True
        elif field in ('allow','disallow'):
            if agents: rules.append((field,value))
            last_was_agent=False
        else:
            last_was_agent=False
    if agents: groups.append((agents,rules))
    return groups


UNRESERVED = set(string.ascii_letters+string.digits+'-._~')


def _normalize(value):
    """RFC 9309 section 2.2.2: compare paths with unreserved characters decoded (%70 is p) and
    other characters percent-encoded in one consistent form, so an encoded path cannot slip past a rule."""
    value=''.join(c if 32<ord(c)<127 else ''.join('%%%02X'%b for b in c.encode('utf-8')) for c in value)
    def fix(m):
        char=chr(int(m[1],16))
        return char if char in UNRESERVED else '%'+m[1].upper()
    return re.sub(r'%([0-9A-Fa-f]{2})',fix,value)


def _pattern(path):
    path=_normalize(path)
    anchored=path.endswith('$')
    body=re.escape(path[:-1] if anchored else path).replace(r'\*','.*')
    return re.compile(body+('$' if anchored else ''))


def cautious_allowed(lines,url):
    """Disallow if any applicable Disallow rule matches; ignore Allow lines."""
    groups=_groups(lines)
    mine=[rules for agents,rules in groups if TOKEN.lower() in agents]
    if not mine: mine=[rules for agents,rules in groups if '*' in agents]
    p=urllib.parse.urlsplit(url)
    target=_normalize((p.path or '/')+('?'+p.query if p.query else ''))
    for rules in mine:
        for field,value in rules:
            if field=='disallow' and value and _pattern(value).match(target):
                return False
    return True


def allowed(text,url):
    lines=text.splitlines()
    if STDLIB_OK:
        rp=urllib.robotparser.RobotFileParser(); rp.parse(lines)
        return rp.can_fetch(TOKEN,url)
    return cautious_allowed(lines,url)
