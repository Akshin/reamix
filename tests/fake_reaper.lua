-- Подделка API REAPER для тестов моста и lib.lua вне REAPER.
-- Моделирует ровно то, что используют bridge/*.lua: треки, плагины с параметрами,
-- историю undo, ExtState и defer. Реальное поведение API проверяется только вживую.

local F = { rev = 100, ext = {}, undo = {}, redo = {}, path = '/music/song/main.rpp' }

local function hz(norm) return string.format('%.1f Hz', 20 * 1000 ^ norm) end
local function khz(norm)
  local f = 20 * 1000 ^ norm
  if f >= 1000 then return string.format('%.2f kHz', f / 1000) end
  return string.format('%.0f Hz', f)
end
local function gain_db(norm) return string.format('%.1f dB', norm * 48 - 24) end
local function ratio(norm) return string.format('%.1f:1', 1 + norm * 19) end

local guid_seq = 0
local function guid()
  guid_seq = guid_seq + 1
  return string.format('{00000000-0000-0000-0000-%012d}', guid_seq)
end

local function fx(name, params)
  return { name = name, guid = guid(), enabled = true, offline = false, params = params }
end

local function track(name, depth, plugins)
  return {
    name = name, guid = guid(), depth = depth or 0, fx = plugins or {},
    vals = {
      D_VOL = 1, D_PAN = 0, D_WIDTH = 1, B_MUTE = 0, I_SOLO = 0, B_PHASE = 0,
      B_MAINSEND = 1, I_NCHAN = 2, I_AUTOMODE = 0, I_FOLDERDEPTH = 0,
    },
  }
end

-- Установленные плагины: имя → чистый экземпляр с параметрами по умолчанию.
local catalog = {
  ['VST: ReaEQ (Cockos)'] = function()
    return {
      { name = 'Freq-Band 1', norm = 0.5, fmt = hz },
      { name = 'Gain-Band 1', norm = 0.5, fmt = gain_db },
    }
  end,
  ['VST3: Pro-C 2 (FabFilter)'] = function()
    return {
      { name = 'Ratio', norm = 0.2, fmt = ratio },
      { name = 'Sidechain HP', norm = 0.1, fmt = khz },
      { name = 'Style', norm = 0, fmt = function(norm) return ({ 'Clean', 'Punch', 'Vocal' })[math.min(3, math.floor(norm * 3) + 1)] end },
    }
  end,
  ['JS: utility/volume'] = function()
    return { { name = 'Adjustment (dB)', norm = 0, fmt = function(v) return string.format('%.1f', v) end } }
  end,
}

local function installed(name)
  local full = name:gsub('^(%w+): ?', '%1: ')
  return fx(full, catalog[full] and catalog[full]() or {})
end

F.master = track('MASTER')
F.tracks = {
  track('Drums'),
  track('Kick', 1, { installed('VST:ReaEQ (Cockos)'), installed('VST3:Pro-C 2 (FabFilter)') }),
  track('Vocal Lead'),
  track('Vocal Double'),
}
F.tracks[1].vals.I_FOLDERDEPTH = 1

local function copy(v)
  if type(v) ~= 'table' then return v end
  local out = {}
  for k, x in pairs(v) do out[k] = copy(x) end
  return out
end

local function touch() F.rev = F.rev + 1 end

local R = {}

function R.time_precise() return os.clock() end
function R.defer(fn) F.deferred = fn end
function R.atexit(fn) F.atexit = fn end
function R.GetExtState(section, key) return F.ext[section .. '/' .. key] or '' end
function R.SetExtState(section, key, value) F.ext[section .. '/' .. key] = value end
function R.RecursiveCreateDirectory(path) os.execute('mkdir -p "' .. path .. '"') return 1 end

function R.EnumProjects() return F, F.path end
function R.GetProjectStateChangeCount() return F.rev end
function R.GetProjectTimeSignature2() return 120, 4 end
function R.GetSetProjectInfo(_, key) return key == 'PROJECT_SRATE' and 48000 or 1 end
function R.GetAudioDeviceInfo() return false, '' end
function R.GetProjectLength() return 205.5 end
function R.IsProjectDirty() return 0 end
function R.CountTempoTimeSigMarkers() return 0 end
function R.CountProjectMarkers() return 1, 0, 1 end
function R.EnumProjectMarkers3() return 1, true, 30, 60, 'Chorus', 1, 0 end

function R.Undo_BeginBlock2() F.saved = { tracks = copy(F.tracks), master = copy(F.master), rev = F.rev } end
function R.Undo_EndBlock2(_, label)
  if F.rev ~= F.saved.rev then
    F.undo[#F.undo + 1] = { label = label, state = F.saved }
    F.redo = {}  -- новая запись в истории стирает redo, как в REAPER
  end
  F.saved = nil
end
function R.Undo_CanUndo2() return F.undo[#F.undo] and F.undo[#F.undo].label or nil end
function R.Undo_CanRedo2() return F.redo[#F.redo] and F.redo[#F.redo].label or nil end
local function swap(from, to)
  local entry = table.remove(from)
  to[#to + 1] = { label = entry.label, state = { tracks = F.tracks, master = F.master } }
  F.tracks, F.master = entry.state.tracks, entry.state.master
  touch()
  return 1
end
function R.Undo_DoUndo2() return swap(F.undo, F.redo) end
function R.Undo_DoRedo2() return swap(F.redo, F.undo) end
function R.PreventUIRefresh() end
function R.UpdateArrange() end
function R.TrackList_AdjustWindows() end

function R.GetMasterTrack() return F.master end
function R.CountTracks() return #F.tracks end
function R.GetTrack(_, i) return F.tracks[i + 1] end
function R.GetTrackGUID(tr) return tr.guid end
function R.GetTrackDepth(tr) return tr.depth end
function R.GetSetMediaTrackInfo_String(tr, _, value, set)
  if set then tr.name = value touch() end
  return true, tr.name
end
function R.GetMediaTrackInfo_Value(tr, key)
  if key == 'IP_TRACKNUMBER' then
    if tr == F.master then return -1 end
    for i, t in ipairs(F.tracks) do if t == tr then return i end end
  end
  return tr.vals[key]
end
function R.SetMediaTrackInfo_Value(tr, key, value) tr.vals[key] = value touch() return true end

function R.GetTrackNumSends() return 0 end
function R.CountTrackEnvelopes() return 0 end
function R.CountTrackMediaItems() return 0 end
function R.TrackFX_GetRecCount() return 0 end
function R.Audio_IsRunning() return 1 end
function R.Audio_Init() end
function R.Audio_Quit() end

function R.TrackFX_GetCount(tr) return #tr.fx end
function R.TrackFX_GetFXName(tr, i) return true, tr.fx[i + 1].name end
function R.TrackFX_GetPreset() return false, '' end
function R.TrackFX_GetFXGUID(tr, i) return tr.fx[i + 1].guid end
function R.TrackFX_GetEnabled(tr, i) return tr.fx[i + 1].enabled end
function R.TrackFX_GetOffline(tr, i) return tr.fx[i + 1].offline end
function R.TrackFX_SetEnabled(tr, i, on) tr.fx[i + 1].enabled = on touch() end
function R.TrackFX_GetNumParams(tr, i) return #tr.fx[i + 1].params end
function R.TrackFX_GetParamName(tr, i, p) return true, tr.fx[i + 1].params[p + 1].name end
function R.TrackFX_GetParam(tr, i, p) return tr.fx[i + 1].params[p + 1].norm, 0, 1 end
function R.TrackFX_GetParamNormalized(tr, i, p) return tr.fx[i + 1].params[p + 1].norm end
function R.TrackFX_GetFormattedParamValue(tr, i, p)
  local param = tr.fx[i + 1].params[p + 1]
  return true, param.fmt(param.norm)
end
function R.TrackFX_FormatParamValueNormalized(tr, i, p, v)
  return true, tr.fx[i + 1].params[p + 1].fmt(v)
end
function R.TrackFX_SetParamNormalized(tr, i, p, v) tr.fx[i + 1].params[p + 1].norm = v touch() return true end
function R.TrackFX_SetParam(tr, i, p, v) tr.fx[i + 1].params[p + 1].norm = v touch() return true end
-- instantiate: -1 — в конец цепочки, -1000 - i — на место i (с нуля).
function R.TrackFX_AddByName(tr, name, _, instantiate)
  if name:find('Missing') then return -1 end
  local at = instantiate and instantiate <= -1000 and math.min(-1000 - instantiate, #tr.fx) or #tr.fx
  table.insert(tr.fx, at + 1, installed(name))
  touch()
  return at
end
function R.TrackFX_GetNamedConfigParm(tr, i, key)
  local plugin = tr.fx[i + 1]
  if key == 'fx_name' then return true, plugin.name end
  if key == 'fx_ident' and plugin.name:find('^JS: ') then return true, (plugin.name:gsub('^JS: ', '')) end
  if key == 'fx_ident' then return true, '/Plug-Ins/' .. plugin.name:gsub('^%w+: ', '') .. '.vst<1' end
  return false, ''
end
function R.InsertTrackAtIndex(i) table.insert(F.tracks, i + 1, track('')) touch() end
function R.DeleteTrack(tr)
  for i, t in ipairs(F.tracks) do if t == tr then table.remove(F.tracks, i) end end
  touch()
end
function R.TrackFX_Delete(tr, i) table.remove(tr.fx, i + 1) touch() return true end

function R.GetTrackStateChunk(tr)
  local lines = { '<TRACK ' .. tr.guid, 'NAME ' .. tr.name }
  for _, key in ipairs({ 'D_VOL', 'D_PAN', 'B_MUTE' }) do lines[#lines + 1] = key .. ' ' .. tr.vals[key] end
  for _, plugin in ipairs(tr.fx) do
    lines[#lines + 1] = 'FX ' .. plugin.name .. ' ' .. tostring(plugin.enabled)
    for _, param in ipairs(plugin.params) do lines[#lines + 1] = string.format('  %s %.17g', param.name, param.norm) end
  end
  lines[#lines + 1] = '>'
  return true, table.concat(lines, '\n')
end

R._fake = F
return R
