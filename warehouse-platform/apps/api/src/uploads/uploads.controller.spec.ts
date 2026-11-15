import { validateSync } from 'class-validator';
import { CreateUploadDto } from './uploads.controller';

function errorsForSize(fileSize: number) {
  const dto = Object.assign(new CreateUploadDto(), { fileSize });
  return validateSync(dto).filter(error => error.property === 'fileSize');
}

describe('resumable upload byte length validation', () => {
  it('accepts an exact size beyond2GiB', () => {
    expect(errorsForSize(3 * 1024 ** 3 + 17)).toEqual([]);
  });
  it.each([0, -1, .5, Number.NaN, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1])(
    'rejects invalid or imprecise bytes %s', fileSize => {
      expect(errorsForSize(fileSize)).not.toHaveLength(0);
    });
});
