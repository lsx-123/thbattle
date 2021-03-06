# -*- coding: utf-8 -*-
from __future__ import absolute_import

# -- stdlib --
from collections import OrderedDict
from copy import copy
import logging

# -- third party --
from gevent import Greenlet, getcurrent
from gevent.pool import Group as GreenletGroup
import gevent

# -- own --
from endpoint import EndpointDied
from game.base import AbstractPlayer, GameEnded, InputTransaction, TimeLimitExceeded
from server.core.event_hooks import ServerEventHooks
from server.core.game_manager import GameManager
from server.subsystem import Subsystem
from utils import log_failure
from utils.gevent_ext import iwait
from utils.stats import stats
import game.base


# -- code --
log = logging.getLogger('Game_Server')


def user_input(players, inputlet, timeout=25, type='single', trans=None):
    '''
    Type can be 'single', 'all' or 'any'
    '''
    assert type in ('single', 'all', 'any')
    assert not type == 'single' or len(players) == 1

    timeout = max(0, timeout)

    g = Game.getgame()
    inputlet.timeout = timeout
    players = list(players)

    if not trans:
        with InputTransaction(inputlet.tag(), players) as trans:
            return user_input(players, inputlet, timeout, type, trans)

    t = {'single': '', 'all': '&', 'any': '|'}[type]
    tag = 'I{0}:{1}:'.format(t, inputlet.tag())

    ilets = {p: copy(inputlet) for p in players}
    for p in players:
        ilets[p].actor = p

    results = {p: None for p in players}
    synctags = {p: g.get_synctag() for p in players}

    orig_players = players[:]
    input_group = GreenletGroup()
    g.gr_groups.add(input_group)
    _input_group = set()

    try:
        inputany_player = None

        def get_input_waiter(p, t):
            try:
                # should be [tag, <Data for Inputlet.parse>]
                # tag likes 'I?:ChooseOption:2345'
                if p.is_npc:
                    ilet = ilets[p]
                    p.handle_user_input(trans, ilet)
                    return ilet.data()
                else:
                    tag, rst = p.client.gexpect(t)
                    return rst
            except EndpointDied:
                return None

        for p in players:
            t = tag + str(synctags[p])
            w = input_group.spawn(get_input_waiter, p, t)
            _input_group.add(w)
            w.player = p
            w.game = g  # for Game.getgame()
            w.gr_name = 'get_input_waiter: p=%r, tag=%s' % (p, t)

        for p in players:
            g.emit_event('user_input_start', (trans, ilets[p]))

        bottom_halves = []

        def flush():
            for t, data, trans, my, rst in bottom_halves:
                g.players.client.gwrite(t, data)
                g.emit_event('user_input_finish', (trans, my, rst))

            bottom_halves[:] = []

        for w in iwait(_input_group, timeout=timeout + 5):
            try:
                rst = w.get()
                p, data = w.player, rst
            except:
                p, data = w.player, None

            my = ilets[p]

            try:
                rst = my.parse(data)
            except:
                log.error('user_input: exception in .process()', exc_info=1)
                # ----- FOR DEBUG -----
                if g.IS_DEBUG:
                    raise
                # ----- END FOR DEBUG -----
                rst = None

            rst = my.post_process(p, rst)

            bottom_halves.append((
                'R{}{}'.format(tag, synctags[p]), data, trans, my, rst
            ))

            players.remove(p)
            results[p] = rst

            if type != 'any':
                flush()

            if type == 'any' and rst is not None:
                inputany_player = p
                break

    except TimeLimitExceeded:
        pass

    finally:
        input_group.kill()

    # flush bottom halves
    flush()

    # timed-out players
    for p in players:
        my = ilets[p]
        rst = my.parse(None)
        rst = my.post_process(p, rst)
        results[p] = rst
        g.emit_event('user_input_finish', (trans, my, rst))
        g.players.client.gwrite('R{}{}'.format(tag, synctags[p]), None)

    if type == 'single':
        return results[orig_players[0]]

    elif type == 'any':
        if not inputany_player:
            return None, None

        return inputany_player, results[inputany_player]

    elif type == 'all':
        return OrderedDict([(p, results[p]) for p in orig_players])

    assert False, 'WTF?!'


class Player(AbstractPlayer):
    dropped = False
    fleed   = False
    is_npc  = False

    def __init__(self, client):
        self.client = client

    def reveal(self, obj_list):
        g = Game.getgame()
        st = g.get_synctag()
        self.client.gwrite('Sync:%d' % st, obj_list)

    def set_dropped(self, v=True):
        self.dropped = v

    def set_fleed(self, v=True):
        self.fleed = v

    def set_client(self, v):
        self.client = v

    def reconnect(self, client):
        self.client = client
        self.dropped = False

    def __data__(self):
        if self.dropped:
            state = 'fleed' if self.fleed else 'dropped'
        else:
            state = self.client.state

        return dict(
            account=self.client.account,
            state=state,
        )

    @property
    def account(self):
        return self.client.account


class NPCPlayer(AbstractPlayer):
    dropped = False
    fleed   = False
    is_npc  = True

    def __init__(self, client, input_handler):
        self.client = client
        self.handle_user_input = input_handler

    def __data__(self):
        return dict(
            account=self.client.account,
            state='ingame',
        )

    def reveal(self, obj_list):
        Game.getgame().get_synctag()

    @property
    def account(self):
        return self.client.account

    def handle_user_input(self, trans, ilet):
        raise Exception('WTF?!')


class Game(Greenlet, game.base.Game):
    suicide = False
    '''
    The Game class, all game mode derives from this.
    Provides fundamental behaviors.

    Instance variables:
        players: list(Players)
        event_handlers: list(EventHandler)

        and all game related vars, eg. tags used by [EventHandler]s and [Action]s
    '''

    CLIENT_SIDE = False
    SERVER_SIDE = True

    def __init__(self):
        Greenlet.__init__(self)
        game.base.Game.__init__(self)

    @log_failure(log)
    def _run(g):
        g.synctag = 0
        g.event_observer = ServerEventHooks()
        g.game = getcurrent()
        mgr = GameManager.get_by_game(g)
        Subsystem.lobby.start_game(mgr)
        try:
            g.process_action(g.bootstrap(mgr.game_params, mgr.consumed_game_items))
        except GameEnded:
            pass
        finally:
            Subsystem.lobby.end_game(mgr)

        assert g.ended

        stats(*g.get_stats())

    @staticmethod
    def getgame():
        return getcurrent().game

    def __repr__(self):
        try:
            gid = str(self.gameid)
        except:
            gid = 'X'

        return '%s:%s' % (self.__class__.__name__, gid)

    def get_synctag(self):
        if self.suicide:
            self.kill()
            return

        self.synctag += 1
        return self.synctag

    def pause(self, time):
        gevent.sleep(time)
