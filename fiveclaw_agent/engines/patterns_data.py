#!/usr/bin/env python3
"""Pattern templates for code generation"""

PATTERNS = {
    # ──────────────────────────────────────────────────────────────────────────
    # GENERIC / FRAMEWORK-AGNOSTIC
    # ──────────────────────────────────────────────────────────────────────────

    "fivem-resource": {
        "description": "Framework-agnostic FiveM resource scaffold — fxmanifest, client, server, config, working command example",
        "variables": {
            "name":        {"required": True,  "description": "Resource name (e.g. my-garage)"},
            "author":      {"default": "Dev",  "description": "Author name"},
            "description": {"default": "FiveM Resource", "description": "Resource description"},
        },
        "files": {
            "fxmanifest.lua": '''fx_version 'cerulean'
game 'gta5'

author '{{author}}'
description '{{description}}'
version '1.0.0'

shared_scripts {
    'config.lua',
}

client_scripts {
    'client/main.lua',
}

server_scripts {
    '@oxmysql/lib/MySQL.lua',
    'server/main.lua',
}

lua54 'yes'
''',
            "config.lua": '''Config = {}

Config.Debug   = false
Config.Command = '{{name}}'   -- chat command to trigger the feature
''',
            "client/main.lua": '''-- ── State ──────────────────────────────────────────────────────────────────
local isReady = false

-- ── Bootstrap ──────────────────────────────────────────────────────────────
AddEventHandler('onClientResourceStart', function(res)
    if res ~= GetCurrentResourceName() then return end
    isReady = true
    if Config.Debug then print('[{{name}}] client ready') end
end)

-- ── Command ────────────────────────────────────────────────────────────────
RegisterCommand(Config.Command, function(src, args)
    if not isReady then return end
    TriggerServerEvent('{{name}}:server:Action', { args = args })
end, false)

-- ── Server → Client ────────────────────────────────────────────────────────
RegisterNetEvent('{{name}}:client:Response', function(data)
    if Config.Debug then print('[{{name}}] received', json.encode(data)) end
end)
''',
            "server/main.lua": '''-- ── Client → Server ────────────────────────────────────────────────────────
RegisterNetEvent('{{name}}:server:Action', function(data)
    local src = source

    -- Basic source validation — always validate on the server
    if type(src) ~= 'number' or src < 1 then return end

    if Config.Debug then
        print(string.format('[{{name}}] Action from %s: %s', src, json.encode(data)))
    end

    -- Do work here, then respond
    TriggerClientEvent('{{name}}:client:Response', src, { ok = true })
end)
''',
        },
    },

    "mysql-storage": {
        "description": "MySQL table definition + Lua CRUD storage module using oxmysql",
        "variables": {
            "table_name":    {"required": True, "description": "Table name (e.g. player_vehicles)"},
            "resource_name": {"required": True, "description": "Resource this module belongs to"},
        },
        "files": {
            "database/{{table_name}}.sql": '''CREATE TABLE IF NOT EXISTS `{{table_name}}` (
    `id`         INT(11)   NOT NULL AUTO_INCREMENT,
    `identifier` VARCHAR(64) NOT NULL,
    `data`       LONGTEXT  DEFAULT NULL,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    INDEX `idx_identifier` (`identifier`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
''',
            "server/storage.lua": '''-- Storage module for {{table_name}}
-- Require in server/main.lua: local Storage = require 'server/storage'

local Storage = {}

function Storage.getAll()
    return MySQL.query.await('SELECT * FROM `{{table_name}}`', {}) or {}
end

function Storage.getByIdentifier(identifier)
    return MySQL.query.await(
        'SELECT * FROM `{{table_name}}` WHERE identifier = ?', { identifier }
    ) or {}
end

function Storage.getById(id)
    local rows = MySQL.query.await(
        'SELECT * FROM `{{table_name}}` WHERE id = ?', { id }
    )
    return rows and rows[1] or nil
end

function Storage.create(identifier, data)
    return MySQL.insert.await(
        'INSERT INTO `{{table_name}}` (identifier, data) VALUES (?, ?)',
        { identifier, json.encode(data) }
    )
end

function Storage.update(id, data)
    return MySQL.update.await(
        'UPDATE `{{table_name}}` SET data = ?, updated_at = NOW() WHERE id = ?',
        { json.encode(data), id }
    )
end

function Storage.delete(id)
    return MySQL.update.await(
        'DELETE FROM `{{table_name}}` WHERE id = ?', { id }
    )
end

return Storage
''',
        },
    },

    "state-bag": {
        "description": "State bag pattern for syncing vehicle or player data across clients",
        "variables": {
            "bag_name":      {"required": True, "description": "State bag key name (e.g. fuelLevel)"},
            "resource_name": {"required": True, "description": "Resource name (used in event names)"},
            "entity_type":   {"required": True, "description": "Entity type: vehicle or player"},
            "default_value": {"default": "0",   "description": "Default value when bag not set"},
        },
        "files": {
            "shared/state_config.lua": '''StateConfig = {
    bagName      = '{{bag_name}}',
    entityType   = '{{entity_type}}',
    defaultValue = {{default_value}},
    replicated   = true,   -- false = server-only
}
''',
            "client/state.lua": '''-- Client: read and react to {{bag_name}} state bag

local cache = {}

-- Read current value (with local cache fallback)
function Get{{bag_name|title}}(entity)
    if not DoesEntityExist(entity) then return StateConfig.defaultValue end
    return cache[entity] or Entity(entity).state[StateConfig.bagName] or StateConfig.defaultValue
end

-- React to changes from any writer (server or peer)
AddStateBagChangeHandler(StateConfig.bagName, nil, function(bagName, key, value)
    local entity = GetEntityFromStateBagName(bagName)
    if entity == 0 then return end

    cache[entity] = value

    TriggerEvent('{{resource_name}}:stateChanged', entity, key, value)

    if Config.Debug then
        print(string.format('[{{resource_name}}] %s.%s = %s', bagName, key, tostring(value)))
    end
end)

-- Purge cache when entity is deleted
AddEventHandler('entityRemoved', function(entity)
    cache[entity] = nil
end)
''',
            "server/state.lua": '''-- Server: write {{bag_name}} state bag

-- Set value on an entity (replicated to all clients if StateConfig.replicated)
function Set{{bag_name|title}}(entity, value)
    if not DoesEntityExist(entity) then
        return false, 'entity does not exist'
    end

    Entity(entity).state:set(StateConfig.bagName, value, StateConfig.replicated)

    if Config.Debug then
        print(string.format('[{{resource_name}}] Set %s=%s on entity %s',
            StateConfig.bagName, tostring(value), entity))
    end

    return true
end

-- Read current value server-side
function Get{{bag_name|title}}(entity)
    if not DoesEntityExist(entity) then return StateConfig.defaultValue end
    return Entity(entity).state[StateConfig.bagName] or StateConfig.defaultValue
end
''',
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # FRAMEWORK-SPECIFIC
    # ──────────────────────────────────────────────────────────────────────────

    "qb-resource": {
        "description": "QBCore resource scaffold — correct player loading, callbacks, and event patterns",
        "variables": {
            "name":        {"required": True,  "description": "Resource name (e.g. qb-garage)"},
            "author":      {"default": "Dev",  "description": "Author name"},
            "description": {"default": "QBCore Resource", "description": "Resource description"},
        },
        "files": {
            "fxmanifest.lua": '''fx_version 'cerulean'
game 'gta5'

author '{{author}}'
description '{{description}}'
version '1.0.0'

shared_scripts {
    '@qb-core/shared/locale.lua',
    'config.lua',
}

client_scripts {
    'client/main.lua',
}

server_scripts {
    '@oxmysql/lib/MySQL.lua',
    'server/main.lua',
}

dependencies {
    'qb-core',
    'oxmysql',
}

lua54 'yes'
''',
            "config.lua": '''Config = {}

Config.Debug = false
''',
            "client/main.lua": '''local QBCore = exports['qb-core']:GetCoreObject()
local PlayerData = {}
local isLoggedIn = false

-- ── Bootstrap ──────────────────────────────────────────────────────────────
AddEventHandler('QBCore:Client:OnPlayerLoaded', function()
    PlayerData  = QBCore.Functions.GetPlayerData()
    isLoggedIn  = true
    if Config.Debug then print('[{{name}}] player loaded:', PlayerData.citizenid) end
end)

AddEventHandler('QBCore:Client:OnPlayerUnload', function()
    PlayerData = {}
    isLoggedIn = false
end)

AddEventHandler('QBCore:Client:OnJobUpdate', function(job)
    PlayerData.job = job
end)

-- ── Server → Client ────────────────────────────────────────────────────────
RegisterNetEvent('{{name}}:client:Notify', function(msg, msgType)
    QBCore.Functions.Notify(msg, msgType or 'primary')
end)

-- ── Example callback usage ─────────────────────────────────────────────────
-- QBCore.Functions.TriggerCallback('{{name}}:server:GetData', function(data)
--     if not data then return end
--     -- use data
-- end)
''',
            "server/main.lua": '''local QBCore = exports['qb-core']:GetCoreObject()

-- ── Helper ─────────────────────────────────────────────────────────────────
local function GetPlayer(src)
    local p = QBCore.Functions.GetPlayer(src)
    if not p then
        print('[{{name}}] GetPlayer failed for source', src)
    end
    return p
end

-- ── Events ─────────────────────────────────────────────────────────────────
RegisterNetEvent('{{name}}:server:Action', function(data)
    local src    = source
    local Player = GetPlayer(src)
    if not Player then return end

    if Config.Debug then
        print(string.format('[{{name}}] Action from %s (%s)',
            Player.PlayerData.name, Player.PlayerData.citizenid))
    end

    TriggerClientEvent('{{name}}:client:Notify', src, 'Action received', 'success')
end)

-- ── Callbacks ──────────────────────────────────────────────────────────────
QBCore.Functions.CreateCallback('{{name}}:server:GetData', function(src, cb)
    local Player = GetPlayer(src)
    if not Player then return cb(nil) end

    cb({ citizenid = Player.PlayerData.citizenid })
end)
''',
        },
    },

    "esx-resource": {
        "description": "ESX resource scaffold — correct ESX object, player loading, xPlayer patterns",
        "variables": {
            "name":        {"required": True,  "description": "Resource name (e.g. esx_garage)"},
            "author":      {"default": "Dev",  "description": "Author name"},
            "description": {"default": "ESX Resource", "description": "Resource description"},
        },
        "files": {
            "fxmanifest.lua": '''fx_version 'cerulean'
game 'gta5'

author '{{author}}'
description '{{description}}'
version '1.0.0'

shared_scripts {
    '@es_extended/imports.lua',
    'config.lua',
}

client_scripts {
    'client/main.lua',
}

server_scripts {
    '@oxmysql/lib/MySQL.lua',
    'server/main.lua',
}

dependencies {
    'es_extended',
    'oxmysql',
}

lua54 'yes'
''',
            "config.lua": '''Config = {}

Config.Debug = false
''',
            "client/main.lua": '''local ESX = exports['es_extended']:getSharedObject()
local isLoggedIn = false

-- ── Bootstrap ──────────────────────────────────────────────────────────────
AddEventHandler('esx:playerLoaded', function(xPlayer)
    isLoggedIn = true
    if Config.Debug then print('[{{name}}] player loaded:', xPlayer.identifier) end
end)

AddEventHandler('esx:onPlayerLogout', function()
    isLoggedIn = false
end)

-- ── Server → Client ────────────────────────────────────────────────────────
RegisterNetEvent('{{name}}:client:Notify', function(msg, msgType)
    ESX.ShowNotification(msg)
end)
''',
            "server/main.lua": '''local ESX = exports['es_extended']:getSharedObject()

-- ── Helper ─────────────────────────────────────────────────────────────────
local function GetPlayer(src)
    local xPlayer = ESX.GetPlayerFromId(src)
    if not xPlayer then
        print('[{{name}}] GetPlayer failed for source', src)
    end
    return xPlayer
end

-- ── Events ─────────────────────────────────────────────────────────────────
RegisterNetEvent('{{name}}:server:Action', function(data)
    local src     = source
    local xPlayer = GetPlayer(src)
    if not xPlayer then return end

    if Config.Debug then
        print(string.format('[{{name}}] Action from %s (%s)',
            xPlayer.getName(), xPlayer.identifier))
    end

    TriggerClientEvent('{{name}}:client:Notify', src, 'Action received')
end)

-- ── Callbacks ──────────────────────────────────────────────────────────────
ESX.RegisterServerCallback('{{name}}:server:GetData', function(src, cb)
    local xPlayer = GetPlayer(src)
    if not xPlayer then return cb(nil) end

    cb({ identifier = xPlayer.identifier, name = xPlayer.getName() })
end)
''',
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # OX_LIB
    # ──────────────────────────────────────────────────────────────────────────

    "ox-lib": {
        "description": "ox_lib integration — callbacks, notify, context menu, and input dialog patterns",
        "variables": {
            "name":          {"required": True, "description": "Resource name"},
            "menu_title":    {"default": "Menu", "description": "Default context menu title"},
        },
        "files": {
            "fxmanifest.lua": '''fx_version 'cerulean'
game 'gta5'

author 'Dev'
description '{{name}} — ox_lib example'
version '1.0.0'

shared_scripts {
    '@ox_lib/init.lua',
    'config.lua',
}

client_scripts {
    'client/main.lua',
}

server_scripts {
    'server/main.lua',
}

dependencies {
    'ox_lib',
}

lua54 'yes'
''',
            "config.lua": '''Config = {}
Config.Debug = false
''',
            "client/main.lua": '''-- ── Notifications ──────────────────────────────────────────────────────────
-- lib.notify({ title = 'Title', description = 'Message', type = 'success' })
-- Types: success | error | inform | warning

-- ── Context menu ───────────────────────────────────────────────────────────
local function OpenMainMenu()
    lib.registerContext({
        id    = '{{name}}_main',
        title = '{{menu_title}}',
        options = {
            {
                title    = 'Option One',
                description = 'Does something',
                icon     = 'circle-check',
                onSelect = function()
                    lib.notify({ title = '{{name}}', description = 'Option One selected', type = 'success' })
                    TriggerServerEvent('{{name}}:server:OptionOne')
                end,
            },
            {
                title    = 'Option Two (with input)',
                icon     = 'pencil',
                onSelect = function()
                    local input = lib.inputDialog('{{menu_title}}', {
                        { type = 'input',  label = 'Name',   placeholder = 'Enter name',   required = true },
                        { type = 'number', label = 'Amount', placeholder = '0',            min = 1, max = 999 },
                    })

                    if not input then return end  -- user cancelled

                    TriggerServerEvent('{{name}}:server:OptionTwo', {
                        name   = input[1],
                        amount = input[2],
                    })
                end,
            },
        },
    })
    lib.showContext('{{name}}_main')
end

-- ── Callback example ───────────────────────────────────────────────────────
local function FetchData()
    local data = lib.callback.await('{{name}}:server:GetData', false)
    if not data then
        lib.notify({ title = 'Error', description = 'Failed to fetch data', type = 'error' })
        return
    end
    if Config.Debug then print('[{{name}}] data:', json.encode(data)) end
    return data
end

-- ── Command ────────────────────────────────────────────────────────────────
RegisterCommand('{{name}}', function()
    OpenMainMenu()
end, false)

-- ── Server → Client ────────────────────────────────────────────────────────
RegisterNetEvent('{{name}}:client:Notify', function(msg, notifyType)
    lib.notify({ title = '{{name}}', description = msg, type = notifyType or 'inform' })
end)
''',
            "server/main.lua": '''-- ── Callback ───────────────────────────────────────────────────────────────
lib.callback.register('{{name}}:server:GetData', function(src)
    -- Basic source check
    if type(src) ~= 'number' or src < 1 then return nil end

    -- Return whatever data the client needs
    return { src = src, timestamp = os.time() }
end)

-- ── Events ─────────────────────────────────────────────────────────────────
RegisterNetEvent('{{name}}:server:OptionOne', function()
    local src = source
    if type(src) ~= 'number' or src < 1 then return end

    -- Handle option one
    TriggerClientEvent('{{name}}:client:Notify', src, 'Option One handled', 'success')
end)

RegisterNetEvent('{{name}}:server:OptionTwo', function(data)
    local src = source
    if type(src) ~= 'number' or src < 1 then return end

    -- Validate
    if type(data) ~= 'table' then return end
    if type(data.name) ~= 'string' or #data.name < 1 then return end
    local amount = tonumber(data.amount)
    if not amount or amount < 1 or amount > 999 then return end

    TriggerClientEvent('{{name}}:client:Notify', src,
        string.format('Got: %s x%d', data.name, amount), 'success')
end)
''',
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # VEHICLE SPAWNER
    # ──────────────────────────────────────────────────────────────────────────

    "vehicle-spawner": {
        "description": "Vehicle spawner — model streaming, network ownership, previous vehicle cleanup",
        "variables": {
            "name":         {"required": True,               "description": "Resource name"},
            "spawn_command":{"default": "spawncar",          "description": "Chat command to spawn"},
            "default_model":{"default": "adder",             "description": "Default vehicle model"},
        },
        "files": {
            "fxmanifest.lua": '''fx_version 'cerulean'
game 'gta5'

author 'Dev'
description '{{name}} — vehicle spawner'
version '1.0.0'

shared_scripts { 'config.lua' }
client_scripts  { 'client/spawner.lua' }
server_scripts  { 'server/main.lua' }

lua54 'yes'
''',
            "config.lua": '''Config = {}

Config.Debug        = false
Config.SpawnCommand = '{{spawn_command}}'
Config.DefaultModel = '{{default_model}}'
Config.SpawnOffset  = vector3(0.0, 5.0, 0.0)   -- offset from player position

-- Allowed models (nil = allow all, table = whitelist)
Config.AllowedModels = nil
''',
            "client/spawner.lua": '''local spawnedVehicle = 0   -- current vehicle spawned by this script

-- ── Helpers ────────────────────────────────────────────────────────────────
local function DeleteSpawned()
    if DoesEntityExist(spawnedVehicle) then
        DeleteVehicle(spawnedVehicle)
    end
    spawnedVehicle = 0
end

local function StreamModel(model)
    if not IsModelValid(model) then return false end
    RequestModel(model)
    local timeout = GetGameTimer() + 5000
    while not HasModelLoaded(model) do
        Wait(100)
        if GetGameTimer() > timeout then
            print('[{{name}}] model stream timeout:', model)
            return false
        end
    end
    return true
end

-- ── Spawner ────────────────────────────────────────────────────────────────
local function SpawnVehicle(modelName)
    local model = GetHashKey(modelName)

    if Config.AllowedModels then
        local allowed = false
        for _, m in ipairs(Config.AllowedModels) do
            if m == modelName then allowed = true; break end
        end
        if not allowed then
            print('[{{name}}] model not in whitelist:', modelName)
            return
        end
    end

    if not StreamModel(model) then
        print('[{{name}}] failed to stream model:', modelName)
        SetModelAsNoLongerNeeded(model)
        return
    end

    DeleteSpawned()

    local pos    = GetEntityCoords(PlayerPedId())
    local spawnPos = vector3(pos.x + Config.SpawnOffset.x,
                             pos.y + Config.SpawnOffset.y,
                             pos.z + Config.SpawnOffset.z)
    local heading = GetEntityHeading(PlayerPedId())

    local veh = CreateVehicle(model, spawnPos.x, spawnPos.y, spawnPos.z, heading, true, false)

    -- Wait for network registration
    local timeout = GetGameTimer() + 3000
    while not NetworkGetEntityIsNetworked(veh) do
        Wait(50)
        if GetGameTimer() > timeout then break end
    end

    SetVehicleOnGroundProperly(veh)
    SetEntityAsNoLongerNeeded(veh)
    SetModelAsNoLongerNeeded(model)
    TaskWarpPedIntoVehicle(PlayerPedId(), veh, -1)

    spawnedVehicle = veh

    if Config.Debug then
        print(string.format('[{{name}}] spawned %s (entity %d, net %d)',
            modelName, veh, NetworkGetNetworkIdFromEntity(veh)))
    end
end

-- ── Commands ───────────────────────────────────────────────────────────────
RegisterCommand(Config.SpawnCommand, function(src, args)
    local model = args[1] or Config.DefaultModel
    SpawnVehicle(model)
end, false)

RegisterCommand(Config.SpawnCommand .. 'del', function()
    DeleteSpawned()
end, false)

-- ── Cleanup on resource stop ────────────────────────────────────────────────
AddEventHandler('onResourceStop', function(res)
    if res ~= GetCurrentResourceName() then return end
    DeleteSpawned()
end)
''',
            "server/main.lua": '''-- Server-side: log spawns if needed
-- The client handles creation directly (singleplayer-style).
-- For a server-authoritative spawn, create the vehicle here and
-- NetworkGetNetworkIdFromEntity → send net ID to client.

AddEventHandler('playerDropped', function(reason)
    -- vehicles created by CreateVehicle on the client are
    -- automatically cleaned up when the player leaves
end)
''',
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    # NUI
    # ──────────────────────────────────────────────────────────────────────────

    "nui-vanilla": {
        "description": "Vanilla HTML/JS NUI — no framework, plain fetch + message API, Lua client integration",
        "variables": {
            "name":         {"required": True,   "description": "Resource name"},
            "title":        {"default": "Panel", "description": "UI window title"},
        },
        "files": {
            "fxmanifest.lua": '''fx_version 'cerulean'
game 'gta5'

author 'Dev'
description '{{name}} — NUI panel'
version '1.0.0'

shared_scripts { 'config.lua' }
client_scripts  { 'client/nui.lua' }
server_scripts  { 'server/main.lua' }

ui_page 'web/index.html'

files {
    'web/index.html',
    'web/style.css',
    'web/app.js',
}

lua54 'yes'
''',
            "config.lua": '''Config = {}
Config.Debug = false
''',
            "web/index.html": '''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{{title}}</title>
  <link rel="stylesheet" href="style.css"/>
</head>
<body>
  <div id="app" class="hidden">
    <div class="panel">
      <div class="panel-header">
        <h2>{{title}}</h2>
        <button id="close-btn">✕</button>
      </div>
      <div class="panel-body">
        <p id="status">Ready.</p>
        <button id="action-btn">Do Action</button>
      </div>
    </div>
  </div>
  <script src="app.js"></script>
</body>
</html>
''',
            "web/style.css": '''* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: 'Segoe UI', sans-serif;
  background: transparent;
  width: 100vw; height: 100vh;
  display: flex; align-items: center; justify-content: center;
}

#app { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; }
#app.hidden { display: none; }

.panel {
  background: #1a1a2e;
  border: 1px solid #2d2d50;
  border-radius: 12px;
  width: 400px;
  color: #e0e0e0;
  overflow: hidden;
  box-shadow: 0 8px 32px rgba(0,0,0,.6);
}

.panel-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 20px;
  background: #16213e;
  border-bottom: 1px solid #2d2d50;
}

.panel-header h2 { font-size: 16px; font-weight: 600; }

.panel-header button {
  background: none; border: none; color: #888;
  font-size: 16px; cursor: pointer; padding: 2px 6px; border-radius: 4px;
}
.panel-header button:hover { background: rgba(255,255,255,.1); color: #fff; }

.panel-body { padding: 20px; display: flex; flex-direction: column; gap: 16px; }

#action-btn {
  background: #3b82f6; color: #fff; border: none;
  padding: 10px 18px; border-radius: 8px; font-size: 14px;
  cursor: pointer; transition: background .15s;
}
#action-btn:hover { background: #2563eb; }

#status { font-size: 13px; color: #9ca3af; }
''',
            "web/app.js": '''// ── NUI helpers ───────────────────────────────────────────────────────────
function post(event, data) {
  return fetch(`https://{{name}}/${event}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data ?? {}),
  }).then(r => r.json()).catch(() => null);
}

// ── UI state ──────────────────────────────────────────────────────────────
const app       = document.getElementById('app');
const statusEl  = document.getElementById('status');

function show(data) {
  app.classList.remove('hidden');
  statusEl.textContent = data?.message ?? 'Ready.';
}

function hide() {
  app.classList.add('hidden');
  post('close');
}

// ── Button handlers ───────────────────────────────────────────────────────
document.getElementById('close-btn').addEventListener('click', hide);

document.getElementById('action-btn').addEventListener('click', async () => {
  const result = await post('action', { value: 42 });
  statusEl.textContent = result?.message ?? 'Done.';
});

// ── Keyboard: Escape closes ────────────────────────────────────────────────
window.addEventListener('keydown', e => {
  if (e.key === 'Escape') hide();
});

// ── FiveM → NUI messages ──────────────────────────────────────────────────
window.addEventListener('message', e => {
  const { type, data } = e.data;
  if (type === 'open') show(data);
  if (type === 'close') app.classList.add('hidden');
});
''',
            "client/nui.lua": '''local nuiOpen = false

-- ── Open ───────────────────────────────────────────────────────────────────
function Open{{title|title}}(data)
    if nuiOpen then return end
    SetNuiFocus(true, true)
    SendNUIMessage({ type = 'open', data = data or {} })
    nuiOpen = true
end

-- ── Close ──────────────────────────────────────────────────────────────────
function Close{{title|title}}()
    if not nuiOpen then return end
    SetNuiFocus(false, false)
    SendNUIMessage({ type = 'close' })
    nuiOpen = false
end

-- ── NUI callbacks ──────────────────────────────────────────────────────────
RegisterNUICallback('close', function(_, cb)
    Close{{title|title}}()
    cb({})
end)

RegisterNUICallback('action', function(data, cb)
    -- data.value is whatever app.js sent
    TriggerServerEvent('{{name}}:server:Action', data)
    cb({ message = 'Action sent to server' })
end)

-- ── Server → Client ────────────────────────────────────────────────────────
RegisterNetEvent('{{name}}:client:Open', function(data)
    Open{{title|title}}(data)
end)

-- ── Command (dev convenience) ───────────────────────────────────────────────
RegisterCommand('{{name}}ui', function()
    if nuiOpen then Close{{title|title}}() else Open{{title|title}}() end
end, false)

-- ── Cleanup ────────────────────────────────────────────────────────────────
AddEventHandler('onResourceStop', function(res)
    if res ~= GetCurrentResourceName() then return end
    if nuiOpen then
        SetNuiFocus(false, false)
        nuiOpen = false
    end
end)
''',
            "server/main.lua": '''-- ── Open panel on a player ─────────────────────────────────────────────────
RegisterNetEvent('{{name}}:server:RequestOpen', function()
    local src = source
    if type(src) ~= 'number' or src < 1 then return end

    TriggerClientEvent('{{name}}:client:Open', src, {
        message = 'Hello from server',
    })
end)

-- ── Receive action from client ──────────────────────────────────────────────
RegisterNetEvent('{{name}}:server:Action', function(data)
    local src = source
    if type(src) ~= 'number' or src < 1 then return end
    if type(data) ~= 'table' then return end

    print(string.format('[{{name}}] action from %d: %s', src, json.encode(data)))
end)
''',
        },
    },

    "nui-react": {
        "description": "React + Vite NUI component — useNuiEvent hook, fetchNui helper, Lua client integration",
        "variables": {
            "name":           {"required": True, "description": "Resource name"},
            "component_name": {"required": True, "description": "Root component name (PascalCase, e.g. Garage)"},
            "feature_name":   {"required": True, "description": "Feature key used in NUI events (camelCase, e.g. garage)"},
        },
        "files": {
            "fxmanifest.lua": '''fx_version 'cerulean'
game 'gta5'

author 'Dev'
description '{{name}} — React NUI'
version '1.0.0'

shared_scripts { 'config.lua' }
client_scripts  { 'client/nui.lua' }
server_scripts  { 'server/main.lua' }

ui_page 'web/dist/index.html'

files {
    'web/dist/index.html',
    'web/dist/**/*',
}

lua54 'yes'
''',
            "config.lua": '''Config = {}
Config.Debug = false
''',
            "web/src/hooks/useNuiEvent.js": '''import { useEffect } from 'react';

/**
 * Listen for a specific NUI message type sent via SendNUIMessage({ type, data })
 */
export function useNuiEvent(type, handler) {
  useEffect(() => {
    const listener = (e) => {
      if (e.data?.type === type) handler(e.data.data);
    };
    window.addEventListener('message', listener);
    return () => window.removeEventListener('message', listener);
  }, [type, handler]);
}
''',
            "web/src/utils/fetchNui.js": '''/**
 * Send a callback to Lua via RegisterNUICallback.
 * Always resolves — never throws — so callers can safely await.
 */
export async function fetchNui(event, data = {}) {
  const resourceName = window.GetParentResourceName?.() ?? '{{name}}';
  try {
    const res = await fetch(`https://${resourceName}/${event}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    return await res.json();
  } catch {
    return null;
  }
}
''',
            "web/src/components/{{component_name}}.jsx": '''import React, { useState, useCallback } from 'react';
import { useNuiEvent } from '../hooks/useNuiEvent';
import { fetchNui } from '../utils/fetchNui';

export function {{component_name}}() {
  const [visible, setVisible] = useState(false);
  const [data,    setData]    = useState(null);
  const [loading, setLoading] = useState(false);

  // Lua: SendNUIMessage({ type = '{{feature_name}}:open', data = {...} })
  useNuiEvent('{{feature_name}}:open', useCallback((payload) => {
    setData(payload);
    setVisible(true);
  }, []));

  // Lua: SendNUIMessage({ type = '{{feature_name}}:close' })
  useNuiEvent('{{feature_name}}:close', useCallback(() => {
    setVisible(false);
  }, []));

  async function handleAction(action, payload) {
    setLoading(true);
    const result = await fetchNui(`{{feature_name}}:${action}`, payload);
    setLoading(false);
    return result;
  }

  function handleClose() {
    fetchNui('{{feature_name}}:close');
    setVisible(false);
  }

  if (!visible) return null;

  return (
    <div style={styles.overlay}>
      <div style={styles.panel}>
        <div style={styles.header}>
          <span style={styles.title}>{{component_name}}</span>
          <button style={styles.closeBtn} onClick={handleClose}>✕</button>
        </div>

        <div style={styles.body}>
          {loading ? (
            <p style={styles.muted}>Loading…</p>
          ) : (
            <>
              <p style={styles.muted}>{data ? JSON.stringify(data) : 'No data'}</p>
              <button
                style={styles.actionBtn}
                disabled={loading}
                onClick={() => handleAction('confirm', { value: 1 })}
              >
                Confirm
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

const styles = {
  overlay:   { position: 'fixed', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(0,0,0,.55)' },
  panel:     { background: '#1a1a2e', border: '1px solid #2d2d50', borderRadius: 12, width: 420, color: '#e0e0e0', overflow: 'hidden', boxShadow: '0 8px 32px rgba(0,0,0,.6)' },
  header:    { display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '14px 20px', background: '#16213e', borderBottom: '1px solid #2d2d50' },
  title:     { fontWeight: 600, fontSize: 15 },
  closeBtn:  { background: 'none', border: 'none', color: '#888', fontSize: 16, cursor: 'pointer', padding: '2px 6px', borderRadius: 4 },
  body:      { padding: 20, display: 'flex', flexDirection: 'column', gap: 14 },
  muted:     { fontSize: 13, color: '#9ca3af' },
  actionBtn: { background: '#3b82f6', color: '#fff', border: 'none', padding: '10px 18px', borderRadius: 8, fontSize: 14, cursor: 'pointer' },
};
''',
            "client/nui.lua": '''local nuiOpen = false

-- ── Open / Close ───────────────────────────────────────────────────────────
function Open{{component_name}}(data)
    if nuiOpen then return end
    SetNuiFocus(true, true)
    SendNUIMessage({ type = '{{feature_name}}:open', data = data or {} })
    nuiOpen = true
end

function Close{{component_name}}()
    if not nuiOpen then return end
    SetNuiFocus(false, false)
    SendNUIMessage({ type = '{{feature_name}}:close' })
    nuiOpen = false
end

-- ── NUI callbacks ──────────────────────────────────────────────────────────
RegisterNUICallback('{{feature_name}}:close', function(_, cb)
    Close{{component_name}}()
    cb({})
end)

RegisterNUICallback('{{feature_name}}:confirm', function(data, cb)
    TriggerServerEvent('{{name}}:server:Confirm', data)
    cb({ ok = true })
end)

-- ── Server → Client ────────────────────────────────────────────────────────
RegisterNetEvent('{{name}}:client:Open', function(data)
    Open{{component_name}}(data)
end)

-- ── Dev command ────────────────────────────────────────────────────────────
RegisterCommand('{{name}}ui', function()
    if nuiOpen then Close{{component_name}}() else Open{{component_name}}() end
end, false)

-- ── Cleanup ────────────────────────────────────────────────────────────────
AddEventHandler('onResourceStop', function(res)
    if res ~= GetCurrentResourceName() then return end
    if nuiOpen then SetNuiFocus(false, false) end
end)
''',
            "server/main.lua": '''RegisterNetEvent('{{name}}:server:RequestOpen', function()
    local src = source
    if type(src) ~= 'number' or src < 1 then return end
    TriggerClientEvent('{{name}}:client:Open', src, { message = 'Hello' })
end)

RegisterNetEvent('{{name}}:server:Confirm', function(data)
    local src = source
    if type(src) ~= 'number' or src < 1 then return end
    if type(data) ~= 'table' then return end
    print(string.format('[{{name}}] Confirm from %d: %s', src, json.encode(data)))
end)
''',
        },
    },
}


def apply_template_variables(template: str, variables: dict) -> str:
    """Apply variables to template with support for |title |lower |upper filters"""
    result = template
    for key, value in variables.items():
        s = str(value)
        result = result.replace(f'{{{{{key}}}}}',         s)
        result = result.replace(f'{{{{{key}|title}}}}',   s.title())
        result = result.replace(f'{{{{{key}|lower}}}}',   s.lower())
        result = result.replace(f'{{{{{key}|upper}}}}',   s.upper())
    return result
