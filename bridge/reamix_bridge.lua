-- reamix: мост между REAPER и CLI.
-- Крутится фоном (defer), ждёт файл .ipc/request.lua, выполняет его как Lua-код
-- и пишет ответ в .ipc/response.json. Протокол — docs/architecture.md.

local VERSION = 1

local source = debug.getinfo(1, 'S').source:gsub('^@', '')
local bridge_dir = source:match('^(.*)[/\\][^/\\]*$')
local root = bridge_dir:match('^(.*)[/\\][^/\\]*$')
local ipc = root .. '/.ipc'

local req_path = ipc .. '/request.lua'
local resp_path = ipc .. '/response.json'
local alive_path = ipc .. '/alive.json'

local json = dofile(bridge_dir .. '/json.lua')

reaper.RecursiveCreateDirectory(ipc, 0)
-- Запрос, оставшийся с прошлого запуска, выполнять нельзя: он мог устареть.
os.remove(req_path)

-- Запущенный позже экземпляр вытесняет предыдущий.
local instance = tostring(reaper.time_precise())
reaper.SetExtState('reamix', 'instance', instance, false)

-- Счётчик изменений проекта на момент последнего снимка «после»: если он не
-- сдвинулся, снимок «до» следующей правки не нужен.
local last_snap = {}

local function write_atomic(path, text)
  local tmp = path .. '.tmp'
  local f = assert(io.open(tmp, 'w'))
  f:write(text)
  f:close()
  os.remove(path)
  assert(os.rename(tmp, path))
end

local function project_path()
  local _, path = reaper.EnumProjects(-1)
  return path or ''
end

local function dirname(path)
  return path:match('^(.*)[/\\][^/\\]*$') or ''
end

-- Активный проект должен лежать в папке, откуда вызвали CLI (или выше неё).
local function check_project(expected)
  local path = project_path()
  if path == '' then return 'активный проект в REAPER не сохранён' end
  local dir, want = dirname(path):lower(), expected:lower()
  if want == dir or want:sub(1, #dir + 1) == dir .. '/' then return nil end
  return 'активный проект в REAPER — ' .. path .. ', а команда вызвана из ' .. expected
end

local function header(text, name)
  return text:match('%-%-@' .. name .. ' ([^\n]*)')
end

local function handle(text)
  local t0 = reaper.time_precise()
  local resp = { id = header(text, 'id') or '' }
  local undo, snap, expected = header(text, 'undo'), header(text, 'snap'), header(text, 'project')

  local function run()
    if expected then
      local mismatch = check_project(expected)
      if mismatch then error(mismatch, 0) end
    end
    -- lib.lua перечитывается на каждый запрос: правки в нём не требуют перезапуска моста.
    local rx = assert(loadfile(bridge_dir .. '/lib.lua'))(json, root)
    local env = setmetatable({ rx = rx, json = json, A = json.array }, { __index = _G })
    local chunk, syntax = load(text, '=request', 't', env)
    if not chunk then error(syntax, 0) end

    local proj = project_path()
    if snap and last_snap[proj] ~= reaper.GetProjectStateChangeCount(0) then
      resp.snap_before = rx.snapshot(snap .. '/before')
    end

    local label
    if undo then
      rx.ensure_no_preview()
      label = 'reamix: ' .. undo
      reaper.Undo_BeginBlock2(0)
      reaper.PreventUIRefresh(1)
    end
    -- Трассировка нужна только для неожиданных ошибок; ожидаемые lib.lua бросает таблицей.
    local ok, result = xpcall(chunk, function(e)
      if type(e) == 'table' and e.reamix then return e.reamix end
      return debug.traceback(tostring(e), 2)
    end)
    if undo then
      reaper.PreventUIRefresh(-1)
      reaper.Undo_EndBlock2(0, label, -1)
      -- Правка упала посередине: откатываем то, что успело примениться.
      if not ok and reaper.Undo_CanUndo2(0) == label then
        reaper.Undo_DoUndo2(0)
        resp.rolled_back = true
      end
      reaper.UpdateArrange()
      reaper.TrackList_AdjustWindows(false)
    end
    if not ok then error(result, 0) end

    if snap then
      resp.snap_after = rx.snapshot(snap .. '/after')
      last_snap[proj] = reaper.GetProjectStateChangeCount(0)
    end
    return result
  end

  local ok, result = xpcall(run, function(e) return tostring(e) end)
  resp.ok = ok
  if ok then resp.result = result else resp.error = result end
  resp.project = project_path()
  resp.rev = reaper.GetProjectStateChangeCount(0)
  resp.ms = math.floor((reaper.time_precise() - t0) * 1000 + 0.5)

  local encoded_ok, encoded = pcall(json.encode, resp)
  if not encoded_ok then
    encoded = json.encode({ id = resp.id, ok = false, error = 'результат не сериализуется: ' .. tostring(encoded) })
  end
  write_atomic(resp_path, encoded)
end

local function heartbeat()
  write_atomic(alive_path, json.encode({
    version = VERSION,
    time = os.time(),
    project = project_path(),
    rev = reaper.GetProjectStateChangeCount(0),
  }))
end

local last_beat = -math.huge

local function tick()
  if reaper.GetExtState('reamix', 'instance') ~= instance then return end
  local f = io.open(req_path, 'r')
  if f then
    local text = f:read('a')
    f:close()
    os.remove(req_path)
    handle(text)
  end
  local now = reaper.time_precise()
  if now - last_beat >= 2 then
    last_beat = now
    heartbeat()
  end
  reaper.defer(tick)
end

reaper.atexit(function()
  if reaper.GetExtState('reamix', 'instance') == instance then os.remove(alive_path) end
end)

tick()
