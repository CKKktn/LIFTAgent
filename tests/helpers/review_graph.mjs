import {execFileSync} from 'node:child_process';
import {existsSync} from 'node:fs';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('../../', import.meta.url));
const windows = process.platform === 'win32';
const environmentPath = windows ? '../../.venv/Scripts/python.exe' : '../../.venv/bin/python';
const environmentPython = fileURLToPath(new URL(environmentPath, import.meta.url));
const fallbackPython = windows ? 'python' : 'python3';
const python = existsSync(environmentPython) ? environmentPython : fallbackPython;
let graph;

export function loadReviewGraph() {
  if (graph === undefined) {
    const output = execFileSync(python, ['-B', '-X', 'utf8', 'tools/export_review_graph.py', '--stdout-json'], {
      cwd: root,
      encoding: 'utf8',
      timeout: 30_000,
    });
    graph = JSON.parse(output);
  }
  return graph;
}
