#!/bin/python
#
# Cryptonote tipbot
# Copyright 2014,2015 moneromooo
# Inspired by "Simple Python IRC bot" by berend
#
# The Cryptonote tipbot is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as published
# by the Free Software Foundation; either version 2, or (at your option)
# any later version.
#

import sys
import os
import socket
import select
import random
import redis
import hashlib
import json
import httplib
import time
import string
import importlib
import re
import tipbot.coinspecs as coinspecs
import tipbot.config as config
from tipbot.log import log_error, log_warn, log_info, log_log
from tipbot.link import *
from tipbot.user import *
from tipbot.group import *
from tipbot.utils import *
from tipbot.redisdb import *
from tipbot.command_manager import *

disabled = False

selected_coin = None
modulenames = []
start_networks = []
argc = 1
while argc < len(sys.argv):
  arg = sys.argv[argc]
  if arg == "-c" or arg == "--coin":
    if argc+1 == len(sys.argv):
      log_error('Usage: tipbot.py [-h|--help] [-m|--module modulename]* -c|--coin <coinname>')
      exit(1)
    argc = argc+1
    selected_coin = sys.argv[argc]
    try:
      log_info('Importing %s coin setup' % selected_coin)
      if not selected_coin in coinspecs.coinspecs:
        log_error('Unknown coin: %s' % selected_coin)
        exit(1)
      for field in coinspecs.coinspecs[selected_coin]:
        setattr(coinspecs, field, coinspecs.coinspecs[selected_coin][field])
    except Exception,e:
      log_error('Failed to load coin setup for %s: %s' % (selected_coin, str(e)))
      exit(1)
  elif arg == "-m" or arg == "--module":
    if argc+1 == len(sys.argv):
      log_error('Usage: tipbot.py [-m|--module modulename]* -c|--coin <coinname>')
      exit(1)
    argc = argc+1
    arg = sys.argv[argc]
    if not arg in modulenames:
      modulenames.append(arg)
  elif arg == "-n" or arg == "--network":
    if argc+1 == len(sys.argv):
      log_error('Usage: tipbot.py [-m|--module modulename]* -c|--coin <coinname>')
      exit(1)
    argc = argc+1
    arg = sys.argv[argc]
    if re.match('[^:]+:.+',arg):
      parts=arg.split(':',1)
      if not parts[1] in modulenames:
        modulenames.append(parts[1])
      start_networks.append({'name':parts[0],'type':parts[1]})
    else:
      if not arg in modulenames:
        modulenames.append(arg)
      start_networks.append({'type':arg})
  elif arg == "-h" or arg == "--help":
    log_info('Usage: tipbot.py [-m|--module modulename]* -c|--coin <coinname>')
    exit(0)
  else:
    log_error('Usage: tipbot.py [-m|--module modulename]* -c|--coin <coinname>')
    exit(1)
  argc = argc + 1

if not selected_coin:
  log_error('Coin setup needs to be specified with -c. See --help')
  exit(1)

sys.path.append(os.path.join('tipbot','modules'))
for modulename in modulenames:
  if modulename in sys.modules:
    log_error('A %s module already exists' % modulename)
    exit(1)
  log_info('Importing %s module' % modulename)
  try:
    __import__(modulename)
  except Exception,e:
    log_error('Failed to load module "%s": %s' % (modulename, str(e)))
    exit(1)



def GetBalance(link,cmd):
  nick=link.user.nick
  try:
    balance,confirming = RetrieveBalance(link)
    sbalance = AmountToString(balance)
    if balance < coinspecs.atomic_units:
      if balance == 0:
        msg="%s's balance is %s" % (nick, sbalance)
      else:
        msg="%s's balance is %s (%.16g %s)" % (nick, sbalance, float(balance) / coinspecs.atomic_units, coinspecs.name)
    else:
      msg="%s's balance is %s" % (nick, sbalance)
    if confirming > 0:
      msg = msg + " (%s awaiting confirmation)" % (AmountToString(confirming))
    link.send(msg)
  except Exception, e:
    log_error('GetBalance: exception: %s' % str(e))
    link.send("An error has occured")

def AddBalance(link,cmd):
  nick=link.user.nick
  if GetParam(cmd,2):
    anick = GetParam(cmd,1)
    amount = GetParam(cmd,2)
  else:
    anick = nick
    amount = GetParam(cmd,1)
  if not amount:
    link.send('usage: !addbalance [<nick>] <amount>')
    return
  try:
    units = long(float(amount)*coinspecs.atomic_units)
  except Exception,e:
    log_error('AddBalance: error converting amount: %s' % str(e))
    link.send('usage: !addbalance [<nick>] <amount>')
    return
  if anick.find(':') == -1:
    network=link.network
    log_info('No network found in %s, using %s from command originator' % (anick,network.name))
    aidentity=Link(network,User(network,anick)).identity()
  else:
    aidentity=anick
  log_info("AddBalance: Adding %s to %s's balance" % (AmountToString(units),aidentity))
  try:
    balance = redis_hincrby("balances",aidentity,units)
  except Exception, e:
    log_error('AddBalance: exception: %s' % str(e))
    link.send( "An error has occured")
    return
  link.send("%s's balance is now %s" % (aidentity,AmountToString(balance)))

def ScanWho(link,cmd):
  link.network.update_users_list(link.group.name if link.group else None)

def GetHeight(link,cmd):
  log_info('GetHeight: %s wants to know block height' % str(link))
  try:
    j = SendDaemonHTMLCommand("getheight")
  except Exception,e:
    log_error('GetHeight: error: %s' % str(e))
    link.send("An error has occured")
    return
  log_log('GetHeight: Got reply: %s' % str(j))
  if not "height" in j:
    log_error('GetHeight: Cannot see height in here')
    link.send("Height not found")
    return
  height=j["height"]
  log_info('GetHeight: height is %s' % str(height))
  link.send("Height: %s" % str(height))

def GetTipbotBalance(link,cmd):
  log_info('%s wants to know the tipbot balance' % str(link))
  try:
    balance, unlocked_balance = RetrieveTipbotBalance()
  except Exception,e:
    link.send("An error has occured")
    return
  pending = long(balance)-long(unlocked_balance)
  if pending == 0:
    log_info("GetTipbotBalance: Tipbot balance: %s" % AmountToString(balance))
    link.send("Tipbot balance: %s" % AmountToString(balance))
  else:
    log_info("GetTipbotBalance: Tipbot balance: %s (%s pending)" % (AmountToString(unlocked_balance), AmountToString(pending)))
    link.send("Tipbot balance: %s (%s pending)" % (AmountToString(unlocked_balance), AmountToString(pending)))

def DumpUsers(link,cmd):
  for network in networks:
    network.dump_users()

def Help(link,cmd):
  module = GetParam(cmd,1)
  if module:
    RunModuleHelpFunction(module,link)
    return

  link.send_private("See available commands with !commands or !commands <modulename>")
  link.send_private("Available modules: %s" % ", ".join(GetModuleNameList(IsAdmin(link))))
  link.send_private("Get help on a particular module with !help <modulename>")
  if coinspecs.web_wallet_url:
    link.send_private("No %s address ? You can use %s" % (coinspecs.name, coinspecs.web_wallet_url))

def Info(link,cmd):
  link.send_private("Info for %s:" % config.tipbot_name)
  link.send_private("Copyright 2014,2015 moneromooo - http://duckpool.mooo.com/tipbot/")
  link.send_private("Type !help, or !commands for a list of commands")
  link.send_private("NO WARRANTY, YOU MAY LOSE YOUR COINS")
  link.send_private("By sending your %s to %s, you are giving up their control" % (coinspecs.name, config.tipbot_name))
  link.send_private("to whoever runs the tipbot. Any tip you make/receive using %s" % config.tipbot_name)
  link.send_private("is obviously not anonymous. %s's wallet may end up corrupt, or be" % config.tipbot_name)
  link.send_private("stolen, the server compromised, etc. While I hope this won't be the case,")
  link.send_private("I will not offer any warranty whatsoever for the use of %s or the" % config.tipbot_name)
  link.send_private("return of any %s. Use at your own risk." % coinspecs.name)
  link.send_private("That being said, I hope you enjoy using it :)")

def InitScanBlockHeight():
  try:
    scan_block_height = redis_get("scan_block_height")
    scan_block_height = long(scan_block_height)
  except Exception,e:
    try:
      redis_set("scan_block_height",0)
    except Exception,e:
      log_error('Failed to initialize scan_block_height: %s' % str(e))

def ShowActivity(link,cmd):
  anick=GetParam(cmd,1)
  achan=GetParam(cmd,2)
  if not anick or not achan:
    link.send('usage: !show_activity <nick> <chan>')
    return
  if anick.find(':') == -1:
    network = link.network
  else:
    parts=anick.split(':')
    network_name=parts[0]
    anick=parts[1]
    network = GetNetworkByName(network_name)
  if network:
    last_activity = network.get_last_active_time(anick,achan)
    if last_activity:
      link.send("%s was active in %s %f seconds ago" % (anick,achan,now-last_activity))
    else:
      link.send("%s was never active in %s" % (anick,achan))
  else:
    link.send("%s is not a valid network" % network)

def SendToLink(link,msg):
  link.send(msg)

def IsRegistered(link,cmd):
  RunRegisteredCommand(link,SendToLink,"You are registered",SendToLink,"You are not registered")

def Reload(link,cmd):
  modulename=GetParam(cmd,1)
  if not modulename:
    link.send("Usage: reload <modulename>")
    return
  if modulename=="builtin":
    link.send("Cannot reload builtin module")
    return
  if not modulename in sys.modules:
    link.send("%s is not a dynamic module" % modulename)
    return
  log_info('Unloading %s module' % modulename)
  UnregisterModule(modulename)
  log_info('Reloading %s module' % modulename)
  try:
    reload(sys.modules[modulename])
    link.send('%s reloaded' % modulename)
  except Exception,e:
    log_error('Failed to load module "%s": %s' % (modulename, str(e)))
    link.send('An error occured')

def Disable(link,cmd):
  global disabled
  disabled = True
  link.send('%s disabled, will require restart' % config.tipbot_name)

def OnIdle():
  if disabled:
    return
  RunIdleFunctions()

def Quit(link,cmd):
  global networks
  msg = ""
  for w in cmd[1:]:
    msg = msg + " " + w
  for network in networks:
    log_info('Quitting %s network' % network.name)
    network.quit()
  networks = []

def OnIdentified(link, identified):
  if disabled:
    log_info('Ignoring identified notification for %s while disabled' % str(link.identity()))
    return
  RunNextCommand(link, identified)

def RegisterCommands():
  RegisterCommand({'module': 'builtin', 'name': 'help', 'parms': '[module]', 'function': Help, 'help': "Displays help about %s" % config.tipbot_name})
  RegisterCommand({'module': 'builtin', 'name': 'commands', 'parms': '[module]', 'function': Commands, 'help': "Displays list of commands"})
  RegisterCommand({'module': 'builtin', 'name': 'isregistered', 'function': IsRegistered, 'help': "show whether you are currently registered with freenode"})
  RegisterCommand({'module': 'builtin', 'name': 'balance', 'function': GetBalance, 'registered': True, 'help': "show your current balance"})
  RegisterCommand({'module': 'builtin', 'name': 'info', 'function': Info, 'help': "infornmation about %s" % config.tipbot_name})

  RegisterCommand({'module': 'builtin', 'name': 'height', 'function': GetHeight, 'admin': True, 'help': "Get current blockchain height"})
  RegisterCommand({'module': 'builtin', 'name': 'tipbot_balance', 'function': GetTipbotBalance, 'admin': True, 'help': "Get current blockchain height"})
  RegisterCommand({'module': 'builtin', 'name': 'addbalance', 'parms': '<nick> <amount>', 'function': AddBalance, 'admin': True, 'help': "Add balance to your account"})
  RegisterCommand({'module': 'builtin', 'name': 'scanwho', 'function': ScanWho, 'admin': True, 'help': "Refresh users list in a channel"})
  RegisterCommand({'module': 'builtin', 'name': 'dump_users', 'function': DumpUsers, 'admin': True, 'help': "Dump users table to log"})
  RegisterCommand({'module': 'builtin', 'name': 'show_activity', 'function': ShowActivity, 'admin': True, 'help': "Show time since a user was last active"})
  RegisterCommand({'module': 'builtin', 'name': 'reload', 'function': Reload, 'admin': True, 'help': "Reload a module"})
  RegisterCommand({'module': 'builtin', 'name': 'disable', 'function': Disable, 'admin': True, 'help': "Disable %s"%config.tipbot_name})
  RegisterCommand({'module': 'builtin', 'name': 'quit', 'function': Quit, 'admin': True, 'help': "Quit"})

def OnCommandProxy(link,cmd):
  if disabled:
    log_info('Ignoring command from %s while disabled: %s' % (str(link.identity()),str(cmd)))
    return
  link.batch_send_start()
  try:
    OnCommand(link,cmd,RunAdminCommand,RunRegisteredCommand)
  except Exception,e:
    log_error('Exception running command %s: %s' % (str(cmd),str(e)))
  link.batch_send_done()

def lower_nick(s,net):
  news = ""
  start_idx = s.find(net)
  if start_idx >= 0:
    start_idx += len(net)
    news = s[:start_idx]
    while start_idx < len(s) and s[start_idx] != ':':
      news = news + s[start_idx].lower()
      start_idx += 1
    news = news + s[start_idx:]
  else:
    news = s
  return news

def MigrateRedis():
  balances=redis_hgetall('balances')
  for balance in balances:
    if balance.find(':') == -1:
      redis_hset('balances','freenode:'+balance,balances[balance])
      redis_hdel('balances',balance)

  keys=redisdb.keys('*')
  for key in keys:
    if key.startswith('dice:stats:') and key.find('freenode:') == -1:
      if key!="dice:stats:" and key!="dice:stats:*":
        parts=key.split(':')
        if len(parts)==3 or (len(parts)==4 and parts[2]=="reset"):
          parts.insert(len(parts)-1,"freenode")
          newkey=":".join(parts)
          log_info('renaming %s to %s' % (key,newkey))
          redisdb.rename(key,newkey)
  for recordtype in ['playerseed', 'serverseed', 'rolls']:
    hname='dice:%s'%recordtype
    keys=redisdb.hgetall(hname)
    for key in keys:
      if key.find(':') == -1:
        newkey='freenode:'+key
        log_info('renaming field %s to %s in %s' % (key,newkey,hname))
        redisdb.hset(hname,newkey,redisdb.hget(hname,key))
        redisdb.hdel(hname,key)

  keys=redisdb.keys('*')
  for key in keys:
    if key.find(":zstats:") >= 0 and key.find(":freenode:") < 0:
      altkey=key.replace(":zstats:",":zstats:freenode:")
      if not redisdb.exists(altkey):
        log_info('copying %s to %s' % (key,altkey))
        redisdb.restore(altkey,0,redisdb.dump(key))
    elif key.endswith(":stats:"):
      altkey=key.replace(":stats:",":stats:freenode:")
      if not redisdb.exists(altkey):
        log_info('copying %s to %s' % (key,altkey))
        redisdb.restore(altkey,0,redisdb.dump(key))

  keys=redisdb.keys()
  for key in keys:
    if key.find("freenode:") >= 0 and not key.endswith("freenode:"):
      altkey=lower_nick(key,"freenode:")
      if altkey == key:
        continue
      for ck in keys:
        if key != ck and lower_nick(ck,"freenode:")==altkey:
          log_error('Found two congruent keys: %s and %s' % (key,ck))
          exit()
      log_info('renaming %s to %s' % (key,altkey))
      redisdb.restore(altkey,0,redisdb.dump(key))
      redisdb.delete(key)
  for hashname in ['balances','paymentid','bookie:1','password','dice:serverseed','dice:playerseed','blackjack:serverseed','blackjack:playerseed']:
    entries=redis_hgetall(hashname)
    for k in entries.keys():
      v=entries[k]
      if v.find("freenode:") >= 0 and not v.endswith("freenode:"):
        altv=lower_nick(v,"freenode:")
        if altv == v:
          continue
        log_info('changing %s:%s value %s to %s' % (hashname,k,v,altv))
        redis_hset(hashname,k,altv)

      if k.find("freenode:") >= 0 and not k.endswith("freenode:"):
        altkey=lower_nick(k,"freenode:")
        if altkey == k:
          continue
        for ck in keys:
          if k != ck and lower_nick(ck,"freenode:")==altkey:
            log_error('Found two congruent keys in %s: %s and %s' % (hashname,k,ck))
            exit()
        log_info('renaming %s key %s to %s' % (hashname,k,altkey))
        redisdb.hset(hashname,altkey,redis_hget(hashname,k))
        redisdb.hdel(hashname,k)

RegisterCommands()
redisdb = connect_to_redis(config.redis_host,config.redis_port)
MigrateRedis()
InitScanBlockHeight()

# TODO: make this be created when the module is loaded
for network_setup in start_networks:
  network_type=network_setup['type']
  if 'name' in network_setup:
    network_name=network_setup['name']
    log_info('Starting "%s" %s network' % (network_name, network_type))
  else:
    network_name=network_type
    log_info('Starting %s network' % network_type)

  name=network_name or network_type
  try:
    network=registered_networks[network_type](name=name)
    network.set_callbacks(OnCommandProxy,OnIdentified)
    if network.connect():
      AddNetwork(network)
  except Exception,e:
    log_error('Error starting %s network: %s' % (name,str(e)))
    exit(1)

while len(networks)>0:
  for network in networks:
    network.update()

  OnIdle()


log_info('shutting down redis')
redisdb.shutdown()
log_info('exiting')
