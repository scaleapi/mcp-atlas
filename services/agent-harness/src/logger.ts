/**
 * Logger for the MCP evaluation server
 * Writes to both console and a timestamped log file in logs/
 */

import * as fs from 'fs'
import * as path from 'path'

type LogLevel = 'info' | 'warn' | 'error' | 'debug'

function createLogFile(): fs.WriteStream {
  const logsDir = path.join(process.cwd(), 'logs')
  if (!fs.existsSync(logsDir)) {
    fs.mkdirSync(logsDir, { recursive: true })
  }
  const now = new Date()
  const timestamp = now.toISOString().replace(/[:.]/g, '-').slice(0, 19)
  const logPath = path.join(logsDir, `server_${timestamp}.log`)
  console.log(`[LOGGER] Writing server logs to: ${logPath}`)
  return fs.createWriteStream(logPath, { flags: 'a' })
}

class Logger {
  private fileStream: fs.WriteStream

  constructor(private level: LogLevel = 'info') {
    this.fileStream = createLogFile()
  }

  private log(level: LogLevel, message: string, meta?: any) {
    const timestamp = new Date().toISOString()
    const logMessage = `[${timestamp}] [${level.toUpperCase()}] ${message}`

    // Write to console (compact for readability)
    if (meta) {
      console.log(logMessage, meta)
    } else {
      console.log(logMessage)
    }

    // Write to file (full detail)
    const fileLine = meta
      ? `${logMessage} ${JSON.stringify(meta)}\n`
      : `${logMessage}\n`
    this.fileStream.write(fileLine)
  }

  info(message: string, meta?: any) {
    this.log('info', message, meta)
  }

  warn(message: string, meta?: any) {
    this.log('warn', message, meta)
  }

  error(message: string, meta?: any) {
    this.log('error', message, meta)
  }

  debug(message: string, meta?: any) {
    if (this.level === 'debug') {
      this.log('debug', message, meta)
    }
  }

  /** Write to log file only (skip console). Use for large payloads. */
  verbose(message: string, meta?: any) {
    const timestamp = new Date().toISOString()
    const logMessage = `[${timestamp}] [VERBOSE] ${message}`
    const fileLine = meta
      ? `${logMessage} ${JSON.stringify(meta)}\n`
      : `${logMessage}\n`
    this.fileStream.write(fileLine)
  }
}

export const logger = new Logger(
  (process.env.LOG_LEVEL as LogLevel) || 'info'
)
