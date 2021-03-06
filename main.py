#!/usr/bin/python3.9
# -*- coding: utf-8 -*-

# if you're interested in development, my test server is
# usually up at 51.161.34.235. just switch the ip of any
# switcher to the one above, toggle it off and on again, and
# you should be connected. registration is done ingame with
# osu!'s built-in registration.
# certificate: https://akatsuki.pw/static/ca.crt

import asyncio
import lzma
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
import cmyui
import orjson # go zoom
from cmyui import Ansi
from cmyui import log
from cmyui.discord import Embed
from cmyui.discord import Webhook
from cmyui.osu import ReplayFrame

from constants.gamemodes import GameMode
from constants.privileges import Privileges
from objects import glob
from objects.achievement import Achievement
from objects.channel import Channel
from objects.clan import Clan
from objects.match import MapPool
from objects.player import Player
from utils.misc import get_press_times
from utils.updater import Updater

if TYPE_CHECKING:
    from objects.score import Score

__all__ = ()

# current version of gulag
glob.version = cmyui.Version(3, 1, 5)

async def on_start() -> None:
    glob.http = aiohttp.ClientSession(json_serialize=orjson.dumps)

    # connect to mysql
    glob.db = cmyui.AsyncSQLPool()
    await glob.db.connect(glob.config.mysql)

    # run the sql updater
    updater = Updater(glob.version)
    await updater.run()
    await updater.log_startup()

    # create our bot & append it to the global player list.
    glob.bot = Player(id=1, name='Aika', priv=Privileges.Normal)
    glob.bot.last_recv_time = float(0x7fffffff)

    glob.players.append(glob.bot)

    # TODO: this section is getting a bit gross.. :P
    # should be moved and probably refactored pretty hard.

    # add all channels from db.
    async for c_res in glob.db.iterall('SELECT * FROM channels'):
        c_res['read_priv'] = Privileges(c_res.get('read_priv', 1))
        c_res['write_priv'] = Privileges(c_res.get('write_priv', 2))
        glob.channels.append(Channel(**c_res))

    # add all mappools from db.
    async for p_res in glob.db.iterall('SELECT * FROM tourney_pools'):
        # overwrite basic types with some class types
        creator = await glob.players.get(id=p_res['created_by'], sql=True)
        p_res['created_by'] = creator # replace id with player object

        pool = MapPool(**p_res)
        await pool.maps_from_sql()
        glob.pools.append(pool)

    # add all clans from db.
    async for c_res in glob.db.iterall('SELECT * FROM clans'):
        # fetch all members from sql
        m_res = await glob.db.fetchall(
            'SELECT id, clan_rank '
            'FROM users '
            'WHERE clan_id = %s',
            c_res['id'], _dict=False
        )

        members = set()

        for p_id, clan_rank in m_res:
            if clan_rank == 3:
                c_res['owner'] = p_id

            members.add(p_id)

        glob.clans.append(Clan(**c_res, members=members))

    # add all achievements from db.
    async for a_res in glob.db.iterall('SELECT * FROM achievements'):
        condition = eval(f'lambda score: {a_res.pop("cond")}')
        achievement = Achievement(**a_res, cond=condition)
        glob.achievements[a_res['mode']].append(achievement)

    """ bmsubmit stuff
    # get the latest set & map id offsets for custom maps.
    maps_res = await glob.db.fetch(
        'SELECT id, set_id FROM maps '
        'WHERE server = "gulag" '
        'ORDER BY id ASC LIMIT 1'
    )

    if maps_res:
        glob.gulag_maps = maps_res
    """

    # add new donation ranks & enqueue tasks to remove current ones.
    # TODO: this system can get quite a bit better; rather than just
    # removing, it should rather update with the new perks (potentially
    # a different tier, enqueued after their current perks).

    async def rm_donor(userid: int, delay: int):
        await asyncio.sleep(delay)

        p = await glob.players.get(id=userid, sql=True)
        await p.remove_privs(Privileges.Donator)

        log(f"{p}'s donation perks have expired.", Ansi.MAGENTA)

    query = ('SELECT id, donor_end FROM users '
             'WHERE donor_end > UNIX_TIMESTAMP()')

    async for donation in glob.db.iterall(query):
        # calculate the delta between now & the exp date.
        delta = donation['donor_end'] - time.time()

        if delta > (60 * 60 * 24 * 30):
            # ignore donations expiring in over a months time;
            # the server should restart relatively often anyways.
            continue

        asyncio.create_task(rm_donor(donation['id'], delta))

PING_TIMEOUT = 300000 // 1000
async def disconnect_inactive() -> None:
    """Actively disconnect users above the
       disconnection time threshold on the osu! server."""
    players_lock = asyncio.Lock()

    while True:
        ctime = time.time()

        async with players_lock:
            for p in glob.players:
                if ctime - p.last_recv_time > PING_TIMEOUT:
                    await p.logout()

        # run this indefinitely
        await asyncio.sleep(PING_TIMEOUT // 3)

# This function is currently pretty tiny and useless, but
# will just continue to expand as more ideas come to mind.
async def analyze_score(score: 'Score') -> None:
    """Analyze a single score."""
    player = score.player

    # open & parse replay files frames
    replay_file = REPLAYS_PATH / f'{score.id}.osr'
    data = lzma.decompress(replay_file.read_bytes())

    frames: list[ReplayFrame] = []

    # ignore seed & blank line at end
    for action in data.decode().split(',')[:-2]:
        if frame := ReplayFrame.from_str(action):
            frames.append(frame)

    if score.mode.as_vanilla == GameMode.vn_taiko:
        # calculate their average press times.
        # NOTE: this does not currently take hit object
        # type into account, making it completely unviable
        # for any gamemode with holds. it's still relatively
        # reliable for taiko though :D.

        press_times = get_press_times(frames)
        config = glob.config.surveillance['hitobj_low_presstimes']

        cond = lambda pt: (sum(pt) / len(pt) < config['value']
                           and len(pt) > config['min_presses'])

        if any(map(cond, press_times.values())):
            # at least one of the keys is under the
            # minimum, log this occurence to Discord.
            webhook_url = glob.config.webhooks['surveillance']
            webhook = Webhook(url=webhook_url)

            embed = Embed(
                title = f'[{score.mode!r}] Abnormally low presstimes detected'
            )

            embed.set_author(
                url = player.url,
                name = player.name,
                icon_url = player.avatar_url
            )

            # TODO: think of a way to organize a thumbnail into config?
            thumb_url = 'https://akatsuki.pw/static/logos/logo.png'
            embed.set_thumbnail(url=thumb_url)

            for key, pt in press_times.items():
                embed.add_field(
                    name = f'Key: {key.name}',
                    value = f'{sum(pt) / len(pt):.2f}ms' if pt else 'N/A',
                    inline = True
                )

            webhook.add_embed(embed)
            await webhook.post(glob.http)

REPLAYS_PATH = Path.cwd() / '.data/osr'
async def run_detections() -> None:
    """Actively run a background thread throughout gulag's
       lifespan; it will pull replays determined as sketch
       from a queue indefinitely."""
    glob.sketchy_queue = asyncio.Queue() # cursed type hint fix
    queue: asyncio.Queue['Score'] = glob.sketchy_queue

    loop = asyncio.get_event_loop()

    while score := await queue.get():
        loop.create_task(analyze_score(score))

if __name__ == '__main__':
    # set cwd to /gulag.
    os.chdir(os.path.dirname(os.path.realpath(__file__)))

    # create /.data and its subdirectories.
    data_path = Path.cwd() / '.data'
    data_path.mkdir(exist_ok=True)

    for sub_dir in ('avatars', 'logs', 'osu', 'osr', 'ss'):
        subdir = data_path / sub_dir
        subdir.mkdir(exist_ok=True)

    app = cmyui.Server(name=f'gulag v{glob.version}',
                       gzip=4, verbose=glob.config.debug)

    # add our domains & tasks
    from domains.cho import domain as cho_domain # c[e4-6]?.ppy.sh
    from domains.osu import domain as osu_domain # osu.ppy.sh
    from domains.ava import domain as ava_domain # a.ppy.sh
    app.add_domains({cho_domain, osu_domain, ava_domain})
    app.add_tasks({on_start(), disconnect_inactive()})

    # if surveillance webhook is enabled,
    # run our auto-detection 'thread' in bg.
    if glob.config.webhooks['surveillance']:
        app.add_task(run_detections())

    app.run(glob.config.server_addr) # blocking call
