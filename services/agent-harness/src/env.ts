// Load environment variables from the repo-root .env file.
// The harness is launched from services/agent-harness (e.g. via `make run-harness`),
// so a bare dotenv.config() would look for .env in the wrong directory. Walk up from
// this file's location to find the nearest .env (the repo root, per the README) so the
// harness boots no matter the current working directory.
// This file must be imported before any other module that reads process.env.
import dotenv from 'dotenv'
import fs from 'fs'
import path from 'path'

function findEnvFile(startDir: string): string | undefined {
  let dir = startDir
  while (true) {
    const candidate = path.join(dir, '.env')
    if (fs.existsSync(candidate)) return candidate
    const parent = path.dirname(dir)
    if (parent === dir) return undefined
    dir = parent
  }
}

const envPath = findEnvFile(__dirname)
dotenv.config(envPath ? { path: envPath } : undefined)
