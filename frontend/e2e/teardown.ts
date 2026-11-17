import { execFileSync } from 'node:child_process'
import path from 'node:path'

export default function teardown() {
  // The Python finally block may not run when Windows terminates a webServer.
  execFileSync(process.env.RAG_PYTHON || 'python', ['-m', 'scripts.cleanup_e2e', '--stop-servers'], {
    cwd: path.resolve('..', 'backend'), stdio: 'inherit',
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  })
}
