import { describe, expect, it } from 'vitest';

import { shortSha } from './format';

describe('shortSha', () => {
  it('keeps the first eight commit characters', () => {
    expect(shortSha('0123456789abcdef0123456789abcdef01234567')).toBe('01234567');
  });
});
