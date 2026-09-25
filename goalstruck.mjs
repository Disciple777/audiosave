#!/usr/bin/env node
/**
 * Goalstruck <-> a markdown file in a project folder.
 *
 * The point of this is not markdown. It is that an agent working in a repo can
 * read what needs doing and tick it off without anyone opening a browser: the
 * file is the interface, and this script is the only thing that knows there is
 * an API behind it.
 *
 *   node goalstruck.mjs login              once per machine -- stores a token
 *   node goalstruck.mjs lists              this account's lists, and their ids
 *   node goalstruck.mjs link <list-id>     writes .goalstruck.json here
 *   node goalstruck.mjs pull               list  -> file  (overwrites the file)
 *   node goalstruck.mjs push               file  -> list  (ticks, unticks, adds)
 *   node goalstruck.mjs status             what push would do, without doing it
 *   node goalstruck.mjs done <id|text>...  tick one Bit by id or title
 *
 * HOW IT REACHES ANYBODY ELSE
 *
 * `npm run build` copies this file into `dist/`, so it deploys with the app and
 * any user can fetch it from the site they already log in to:
 *
 *     https://guatermelon.com/goalstruck/goalstruck.mjs
 *
 * That is the whole distribution story, and it is why there are no
 * dependencies and no build step -- one file that runs where it lands. It is
 * deliberately not on npm: publishing means a package name, a release process
 * and a version to keep in step with an API it is shipped beside anyway.
 *
 * WHY THE FILE IS THE SOURCE OF TRUTH FOR TICKS, AND THE LIST FOR EVERYTHING ELSE
 *
 * A two-way sync needs a conflict rule, and the honest one here is narrow.
 * `push` only ever changes three things: a checkbox that was ticked in the
 * file, a checkbox that was unticked, and a line that has no id yet. Titles,
 * order, nesting, priorities and anything else are read from the list and
 * rewritten into the file by `pull` -- they are never read back out of it. So
 * editing prose in the file is safe in the sense that it will be discarded, and
 * never in the sense that it will be applied.
 *
 * That is deliberate. The alternative -- a real diff over a tree -- has to
 * guess whether a changed line is a rename or a delete plus an add, and getting
 * that wrong deletes somebody's work. Ticking a box cannot.
 *
 * WHY IDS ARE IN HTML COMMENTS
 *
 * A line has to be matchable after it has been edited, and titles are not
 * stable enough: two "Fix the tests" in one file, or one of them reworded, and
 * a title match silently ticks the wrong Bit. So each line carries its Bit id
 * in a trailing `<!--gs:123-->`, which every markdown renderer hides and every
 * text editor shows. A line without one is new, which is exactly what you want
 * when you type a line by hand.
 *
 * NO DEPENDENCIES
 *
 * Node 18+, `fetch` and `node:` builtins only. This ends up copied into other
 * repos, and an npm install is a reason not to bother.
 */

import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'
import { createInterface } from 'node:readline/promises'
import { stdin, stdout, argv, exit } from 'node:process'

const PROJECT_CONFIG = '.goalstruck.json'
const CREDENTIALS = join(homedir(), '.goalstruck', 'credentials.json')
const DEFAULT_API = 'https://guatermelon.com/goalstruck/backend/public'
const DEFAULT_FILE = 'GOALSTRUCK.md'

/** `- [ ] Title <!--gs:123-->`, at any indent, with the id optional. */
const TASK_LINE = /^(\s*)[-*]\s+\[( |x|X)\]\s+(.*?)\s*(?:<!--\s*gs:(\d+)\s*-->)?\s*$/

/* ---------------------------------------------------------------------------
 * Config and credentials
 *
 * Split on purpose. The project config describes *this repo* and is meant to be
 * committed; the token is per machine and must never be, which is why it lives
 * in the home directory rather than in a gitignored file next to it that
 * somebody will eventually commit anyway.
 * ------------------------------------------------------------------------- */

async function readJson(path, fallback = null) {
  try {
    return JSON.parse(await readFile(path, 'utf8'))
  } catch {
    return fallback
  }
}

async function projectConfig() {
  const path = resolve(PROJECT_CONFIG)
  const config = await readJson(path)

  if (config === null) {
    fail(
      `No ${PROJECT_CONFIG} in ${process.cwd()}\n\n` +
        `  node goalstruck.mjs lists          to see your lists\n` +
        `  node goalstruck.mjs link <list-id> to connect this folder to one`,
    )
  }

  return { api: DEFAULT_API, file: DEFAULT_FILE, ...config }
}

async function token() {
  // The environment wins, so CI and a throwaway shell never have to write a
  // file, and so a wrong stored token can be stepped around in one command.
  if (process.env.GOALSTRUCK_TOKEN) {
    return process.env.GOALSTRUCK_TOKEN
  }

  const stored = await readJson(CREDENTIALS)

  if (stored?.token) {
    return stored.token
  }

  fail('Not signed in. Run: node goalstruck.mjs login')
}

/* ---------------------------------------------------------------------------
 * API
 * ------------------------------------------------------------------------- */

async function api(config, method, path, { body, query, auth = true } = {}) {
  const url = new URL(config.api.replace(/\/$/, '') + path)

  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined) {
      url.searchParams.set(key, String(value))
    }
  }

  const headers = { Accept: 'application/json' }

  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
  }

  if (auth) {
    headers.Authorization = `Bearer ${await token()}`
  }

  let response

  try {
    response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch (error) {
    fail(`Could not reach ${url.origin}: ${error.message}`)
  }

  const text = await response.text()
  let payload = null

  try {
    payload = text === '' ? null : JSON.parse(text)
  } catch {
    // A non-JSON body means PHP emitted a warning or an error page. The status
    // is still the useful signal.
  }

  if (!response.ok) {
    // Only a *stored* token can have gone stale. During login there is no token
    // yet, and telling somebody who mistyped their password to sign in again --
    // which is what they were doing -- is the least helpful thing available.
    if (response.status === 401 && auth) {
      fail('That token is not valid any more. Run: node goalstruck.mjs login')
    }

    fail(payload?.message ?? `${method} ${path} failed (${response.status})`)
  }

  return payload
}

/**
 * The one list this folder is linked to.
 *
 * There is no "fetch one list" endpoint, so this reads the overview and picks.
 * Deliberately not adding one: the overview is a handful of rows, the endpoint
 * already exists and is already tested, and a second way to read a list is a
 * second thing to keep true.
 */
async function fetchList(config) {
  const payload = await api(config, 'GET', '/api/lists', {
    query: { include_completed: 1 },
  })

  const found = (payload.lists ?? []).find((list) => Number(list.id) === Number(config.list))

  if (found === undefined) {
    fail(
      `List ${config.list} is not in this account any more.\n` +
        `Run \`node goalstruck.mjs lists\` and re-link with \`... link <list-id>\`.`,
    )
  }

  return found
}

/* ---------------------------------------------------------------------------
 * Markdown
 * ------------------------------------------------------------------------- */

/**
 * One line per Bit, in the list's own order and nesting.
 *
 * The shapes match `src/lib/checklistText.ts`, because the same file is going
 * to be pasted into a chat or an issue and it should look like the app's own
 * copy output when it lands there.
 */
function toMarkdown(list) {
  const lines = []

  function walk(items, depth) {
    for (const item of items) {
      const pad = '  '.repeat(depth)
      const id = `<!--gs:${item.id}-->`

      if (item.bit_type === 'heading') {
        lines.push('', `${'#'.repeat(Math.min(depth + 2, 4))} ${item.title} ${id}`, '')
      } else if (item.bit_type === 'note') {
        lines.push(`${pad}${item.title} ${id}`)
      } else if (item.bit_type === 'toggle') {
        lines.push(`${pad}**${item.title}** ${id}`)
      } else {
        lines.push(`${pad}- [${item.status === 'completed' ? 'x' : ' '}] ${item.title} ${id}`)
      }

      walk(item.children ?? [], depth + 1)
    }
  }

  walk(list.items ?? [], 0)

  return [
    `# ${list.title}`,
    '',
    '<!-- Synced from Goalstruck. Tick a box, then: node goalstruck.mjs push',
    '     A line with no `gs:` comment becomes a new Bit when you push.',
    '     Everything else here is rewritten by `node goalstruck.mjs pull`. -->',
    '',
    ...lines,
  ]
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trimEnd() + '\n'
}

/** Every task line in the file, with whatever id it carries. */
function parseMarkdown(text) {
  const rows = []

  text.split(/\r?\n/).forEach((line, index) => {
    const match = TASK_LINE.exec(line)

    if (match === null) {
      return
    }

    const [, indent, box, title, id] = match

    if (title.trim() === '') {
      return
    }

    rows.push({
      line: index,
      indent: indent.length,
      checked: box.toLowerCase() === 'x',
      title: title.trim(),
      id: id === undefined ? null : Number(id),
    })
  })

  return rows
}

/** Every Bit in the list, flattened, so a row can be looked up by id. */
function flatten(list) {
  const found = new Map()

  function walk(items) {
    for (const item of items) {
      found.set(Number(item.id), item)
      walk(item.children ?? [])
    }
  }

  walk(list.items ?? [])

  return found
}

/**
 * What `push` would do.
 *
 * Split out from doing it so `status` and `push` cannot disagree about what
 * counts as a change -- the failure mode where a dry run says one thing and the
 * real one does another is worse than having no dry run.
 */
function plan(rows, byId) {
  const complete = []
  const reopen = []
  const create = []
  const missing = []

  for (const row of rows) {
    if (row.id === null) {
      create.push(row)
      continue
    }

    const bit = byId.get(row.id)

    if (bit === undefined) {
      // The Bit was deleted in the app, or the file came from another list.
      // Reported rather than recreated: recreating it would resurrect work
      // somebody deliberately threw away.
      missing.push(row)
      continue
    }

    const done = bit.status === 'completed'

    if (row.checked && !done) {
      complete.push({ row, bit })
    } else if (!row.checked && done) {
      reopen.push({ row, bit })
    }
  }

  return { complete, reopen, create, missing }
}

/* ---------------------------------------------------------------------------
 * Commands
 * ------------------------------------------------------------------------- */

const commands = {
  async login(...args) {
    // The API base is a flag, not a question. Almost everybody is on the one
    // hosted instance, and asking first meant the first thing this tool ever
    // said to a new user was a question only a self-hoster could answer.
    const flag = args.indexOf('--api')
    const base = flag === -1 ? DEFAULT_API : args[flag + 1]

    if (base === undefined) {
      fail('--api needs a URL after it.')
    }

    say(`Signing in to ${base}`)
    say('This is your Goalstruck account -- the same one you use in the browser.')

    const ask = await prompter()
    const email = await ask.line('Email: ')
    const password = await ask.secret('Password: ')

    ask.close()

    const session = await api({ api: base }, 'POST', '/api/auth/login', {
      body: { email, password },
      auth: false,
    })

    if (!session?.token) {
      fail('The server did not return a token.')
    }

    await mkdir(join(homedir(), '.goalstruck'), { recursive: true })
    await writeFile(CREDENTIALS, JSON.stringify({ api: base, token: session.token }, null, 2))

    say('')
    say(`Signed in. Token stored in ${CREDENTIALS}`)
    say('That file is your account -- keep it out of any repo.')
    say('')
    say('Next: node goalstruck.mjs lists')
  },

  async lists() {
    const stored = await readJson(CREDENTIALS, {})
    const config = { api: stored.api ?? DEFAULT_API }
    const sources = await api(config, 'GET', '/api/lanes/sources')

    if ((sources.lists ?? []).length === 0) {
      say('No lists in this account yet.')
      return
    }

    for (const list of sources.lists) {
      say(`${String(list.id).padStart(6)}  ${list.title}`)
    }
  },

  async link(listId, file) {
    if (listId === undefined) {
      fail('Which list? Run `node goalstruck.mjs lists` for the ids.')
    }

    const stored = await readJson(CREDENTIALS, {})
    const config = {
      api: stored.api ?? DEFAULT_API,
      list: Number(listId),
      file: file ?? DEFAULT_FILE,
    }

    // Fetched before writing, so linking to a list that does not exist fails
    // here rather than on the first pull in a week's time.
    const list = await fetchList(config)

    await writeFile(resolve(PROJECT_CONFIG), JSON.stringify(config, null, 2) + '\n')

    say(`Linked to "${list.title}" (${config.list}).`)
    say(`Wrote ${PROJECT_CONFIG}. Now: node goalstruck.mjs pull`)
  },

  async pull() {
    const config = await projectConfig()
    const list = await fetchList(config)

    await writeFile(resolve(config.file), toMarkdown(list))

    const items = flatten(list)
    const done = [...items.values()].filter((item) => item.status === 'completed').length

    say(`${config.file}: ${items.size} Bits, ${done} done.`)
  },

  async status() {
    const config = await projectConfig()
    const { complete, reopen, create, missing } = await pending(config)

    if (complete.length + reopen.length + create.length === 0) {
      say(`${config.file} matches the list.`)
    }

    for (const { bit } of complete) say(`  tick    ${bit.title}`)
    for (const { bit } of reopen) say(`  untick  ${bit.title}`)
    for (const row of create) say(`  add     ${row.title}`)
    for (const row of missing) say(`  gone    ${row.title} (Bit ${row.id} no longer exists)`)
  },

  async push() {
    const config = await projectConfig()
    const { complete, reopen, create, missing } = await pending(config)

    for (const { bit } of complete) {
      await api(config, 'PATCH', '/api/bits/state', {
        body: { bit_id: bit.id, status: 'completed' },
      })
      say(`ticked   ${bit.title}`)
    }

    for (const { bit } of reopen) {
      await api(config, 'PATCH', '/api/bits/state', {
        body: { bit_id: bit.id, status: 'active' },
      })
      say(`unticked ${bit.title}`)
    }

    for (const row of create) {
      const created = await api(config, 'POST', '/api/lists/items', {
        body: { list_bit_id: config.list, title: row.title },
      })
      say(`added    ${row.title} (${created.bit_id})`)
    }

    for (const row of missing) {
      warn(`skipped  ${row.title} -- Bit ${row.id} no longer exists`)
    }

    if (complete.length + reopen.length + create.length === 0) {
      say('Nothing to push.')
      return
    }

    // Pulled straight back so the file carries the ids of anything just
    // created. Without this, a second push would create them all over again.
    await commands.pull()
  },

  async done(...terms) {
    if (terms.length === 0) {
      fail('Which Bit? An id, or enough of its title to match one line.')
    }

    const config = await projectConfig()
    const list = await fetchList(config)
    const items = [...flatten(list).values()]

    for (const term of terms) {
      const matches = /^\d+$/.test(term)
        ? items.filter((item) => Number(item.id) === Number(term))
        : items.filter((item) => item.title.toLowerCase().includes(term.toLowerCase()))

      if (matches.length === 0) {
        warn(`no match: ${term}`)
        continue
      }

      if (matches.length > 1) {
        // Refused rather than guessed. "Tick the first thing that matched" is
        // how the wrong task gets closed.
        warn(`ambiguous: ${term}`)
        for (const item of matches) warn(`    ${item.id}  ${item.title}`)
        continue
      }

      await api(config, 'PATCH', '/api/bits/state', {
        body: { bit_id: matches[0].id, status: 'completed' },
      })
      say(`ticked   ${matches[0].title}`)
    }

    await commands.pull()
  },
}

async function pending(config) {
  const path = resolve(config.file)

  if (!existsSync(path)) {
    fail(`${config.file} is not here yet. Run: node goalstruck.mjs pull`)
  }

  const [text, list] = await Promise.all([readFile(path, 'utf8'), fetchList(config)])

  return plan(parseMarkdown(text), flatten(list))
}

/* ------------------------------------------------------------------------- */

/**
 * Asks for a line, and for a line that must not be echoed.
 *
 * Two implementations because there are two situations, and the obvious single
 * one is broken in both. `readline/promises` settles only the *first*
 * `question()` when stdin is a pipe -- every later one hangs until the process
 * gives up -- so a scripted `login` could never get past the email. And
 * readline has no way to hide what is typed, so an interactive password would
 * sit in the scrollback and the shell history of whoever ran it.
 *
 * So: readline plus raw mode when there is a terminal, and when there is not,
 * the input is read to the end and handed out a line at a time. Raw mode is
 * used directly rather than through readline's private `_writeToOutput`, which
 * is what most snippets reach for and is not a surface to depend on.
 */
async function prompter() {
  if (stdin.isTTY) {
    const rl = createInterface({ input: stdin, output: stdout })

    return {
      line: async (prompt) => (await rl.question(prompt)).trim(),
      secret: (prompt) => hidden(prompt),
      close: () => rl.close(),
    }
  }

  let text = ''

  for await (const chunk of stdin) {
    text += chunk
  }

  const lines = text.split(/\r?\n/)

  return {
    line: async (prompt) => {
      const value = lines.shift() ?? ''

      // Echoed, because a piped run has no other record of what it answered.
      stdout.write(prompt + value + '\n')

      return value.trim()
    },
    secret: async (prompt) => {
      const value = lines.shift() ?? ''

      stdout.write(prompt + '\n')

      return value
    },
    close: () => {},
  }
}

/** One line from a real terminal, with nothing drawn as it is typed. */
async function hidden(prompt) {
  stdout.write(prompt)

  const wasRaw = stdin.isRaw

  stdin.setRawMode(true)
  stdin.resume()

  let value = ''

  try {
    for await (const chunk of stdin) {
      let done = false

      for (const code of chunk) {
        if (code === 3) {
          // Ctrl-C. The finally puts the terminal back before this exits -- a
          // shell left in raw mode after a quit is a broken shell.
          stdout.write('\n')
          exit(130)
        }

        if (code === 13 || code === 10) {
          done = true
          break
        }

        if (code === 127 || code === 8) {
          value = value.slice(0, -1)
          continue
        }

        if (code >= 32) {
          value += String.fromCharCode(code)
        }
      }

      if (done) {
        break
      }
    }
  } finally {
    stdin.setRawMode(wasRaw)
    stdin.pause()
  }

  stdout.write('\n')

  return value
}

function say(message) {
  stdout.write(message + '\n')
}

function warn(message) {
  stdout.write(message + '\n')
}

function fail(message) {
  stdout.write(message + '\n')
  exit(1)
}

const [name, ...args] = argv.slice(2)
const command = commands[name ?? '']

if (command === undefined) {
  say(
    [
      'node goalstruck.mjs <command>',
      '',
      '  login [--api <url>]    sign in once, per machine',
      '  lists                  your lists and their ids',
      '  link <list-id> [file]  connect this folder to a list',
      '  pull                   list -> file',
      '  push                   file -> list (ticks, unticks, adds)',
      '  status                 what push would do',
      '  done <id|text>...      tick one Bit',
      '',
      'Start with login, then lists, then link.',
    ].join('\n'),
  )
  exit(name === undefined ? 0 : 1)
}

await command(...args)
