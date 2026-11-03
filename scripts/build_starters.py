"""Regenerate the assistant starter and Claude skill files from the canonical sources:
docs/ASSISTANT_GUIDE.md, docs/FRAMEWORK.md and sources.json.

    python scripts/build_starters.py          # rewrite generated sections
    python scripts/build_starters.py --check  # exit 1 if anything is out of date
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL = ROOT/'.claude'/'skills'/'alien-search'
STANDARD = ROOT/'skills'/'alien-search'


def cell(text):
    return str(text).replace('|','/').replace('\n',' ')


def register_markdown(data):
    out=['## Public source register','',
         'Historical locators seeded '+data['as_of']+'; not newly verified. Each entry gives the source, agency and public URL, then earlier review notes: "Review" says how much was read and "Locator" gives verified pages. Treat notes as leads to re-check, not current verification. Where a note says a file was retained, saved or preserved, that copy belonged to the original reviewer and is not included: obtain the public original yourself.','']
    for s in data['sources']:
        parts=['**%s** %s — %s — %s'%(s['id'],cell(s['title']),cell(s['agency']),s['url'])]
        if s.get('aliases'): parts.append('Also: '+', '.join(s['aliases'])+'.')
        parts.append('Date: %s.'%s['date'].rstrip('.'))
        parts.append('Review: %s.'%s['scope'].rstrip('.'))
        if s.get('locator'): parts.append('Locator: %s.'%s['locator'].rstrip('.'))
        parts.append(s['note'])
        out.append('- '+' '.join(parts))
    out+=['','### Alternate routes for specific sources','']
    grouped={}  # identical routes shared by several sources are listed once
    for s in data['sources']:
        for alt in s.get('alternates',[]):
            grouped.setdefault((alt['kind'],alt['url'],alt['note'],alt.get('needs_key')),[]).append(s['id'])
    for (kind,url,note,needs_key),ids in grouped.items():
        key=' Needs your own '+needs_key+'.' if needs_key else ''
        out.append('- %s (%s): %s — %s%s'%(', '.join(ids),kind,url or '(search the archive by the original URL)',note,key))
    out+=['','### Known gaps (not in the register yet)','']
    for gap in data['known_gaps']:
        out.append('- %s: %s'%(gap['topic'],gap['status']))
    out+=['','New sources: '+data['new_source_ids'],'','### Archives for blocked, moved or missing pages','']
    for route in data['archive_routes']:
        out.append('- %s%s — %s'%(route['name'],' ('+route['url']+')' if route.get('url') else '',route['note']))
    return '\n'.join(out)+'\n'


def demoted(name,drop_intro=False):
    text=(ROOT/'docs'/name).read_text(encoding='utf-8')
    if drop_intro:
        # The starter's own first lines already say what this is and that it is unofficial; keep the edition line.
        title,rest=text.split('\n',1)
        rest='\n'.join(l for l in rest.split('\n## ',1)[0].splitlines() if l.startswith('Edition'))+'\n\n## '+rest.split('\n## ',1)[1]
        text=title+'\n\n'+rest
    # Demote headings by one level so they nest under the starter's title.
    return re.sub(r'^(#+) ',lambda m:'#'+m.group(1)+' ',text,flags=re.M)


def fill(text,name,body):
    pattern=re.compile(r'(<!-- gen:%s -->\n).*?(<!-- /gen:%s -->)'%(name,name),re.S)
    if not pattern.search(text): raise SystemExit('Missing generated-section markers for '+name)
    return pattern.sub(lambda m:m.group(1)+body+m.group(2),text)


def outputs():
    data=json.loads((ROOT/'sources.json').read_text(encoding='utf-8'))
    starter=(ROOT/'ASSISTANT_STARTER.md').read_text(encoding='utf-8')
    starter=fill(starter,'guide',demoted('ASSISTANT_GUIDE.md'))
    starter=fill(starter,'framework',demoted('FRAMEWORK.md',drop_intro=True))
    starter=fill(starter,'sources',register_markdown(data))
    files={
        ROOT/'ASSISTANT_STARTER.md':starter,
        SKILL/'guide.md':(ROOT/'docs'/'ASSISTANT_GUIDE.md').read_text(encoding='utf-8'),
        SKILL/'framework.md':(ROOT/'docs'/'FRAMEWORK.md').read_text(encoding='utf-8'),
        SKILL/'sources.md':'# Alien Search source register\n\nGenerated from sources.json. Unofficial; not affiliated with or endorsed by any government agency.\n\n'+register_markdown(data),
        # The skill ZIP is shared on its own, so it carries the MIT notice the license requires.
        SKILL/'LICENSE':(ROOT/'LICENSE').read_text(encoding='utf-8'),
    }
    # skills/alien-search/ is the standard Agent Skills location that installers such as
    # `gh skill install` search by default; .claude/skills/ is where Claude Code looks inside a
    # checkout. Both hold the same five files; SKILL.md is written by hand in .claude/skills/.
    files[STANDARD/'SKILL.md']=(SKILL/'SKILL.md').read_text(encoding='utf-8')
    for name in ('guide.md','framework.md','sources.md','LICENSE'):
        files[STANDARD/name]=files[SKILL/name]
    return files


def main():
    check='--check' in sys.argv[1:]
    stale=[]
    for path,content in outputs().items():
        current=path.read_text(encoding='utf-8') if path.exists() else None
        if current!=content:
            stale.append(str(path.relative_to(ROOT)))
            if not check:
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(content,encoding='utf-8')
    if check and stale:
        print('Out of date: '+', '.join(stale)+' (run scripts/build_starters.py)'); return 1
    print('Up to date' if not stale else 'Updated: '+', '.join(stale))
    return 0


if __name__=='__main__':
    sys.exit(main())
