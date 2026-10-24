import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runtimeTotals } from './runtimeData.js';

test('requests are summed per model across runtime groups', () => {
  const groups = [{ models: [
    { id: 'credit', requests: { total: 10, completed: 8, failed: 1, in_progress: 1 } },
    { id: 'churn', requests: { total: 5, completed: 5, failed: 0, in_progress: 0 } },
  ] }, { models: [{ id: 'embedding', requests: { total: 20, completed: 18, failed: 2, in_progress: 0 } }] }];
  assert.deepEqual(runtimeTotals(groups), { total: 35, completed: 31, failed: 3, in_progress: 1, model_count: 3, complete: true });
});

test('unavailable runtime counters stay unavailable rather than becoming zero', () => {
  assert.equal(runtimeTotals(null).complete, false);
  assert.equal(runtimeTotals([{ models: [{ id: 'credit', requests: null }] }]).complete, false);
  assert.equal(runtimeTotals([]).complete, true);
});
