const { test } = require('node:test');
const assert = require('node:assert/strict');

const load = () => import('../app/static/api.js');

test('a NAS filesystem denial is a permission error', async () => {
  const { ApiError } = await load();
  assert.equal(new ApiError('Permission denied', 403).isPermission, true);
});

test('a policy 403 is not reported as missing NAS write access', async () => {
  const { ApiError } = await load();
  assert.equal(new ApiError("Personal folders can't be shared with a public link", 403).isPermission, false);
  assert.equal(new ApiError('Only NAS admins can empty the Recycle bin for everyone', 403).isPermission, false);
});
