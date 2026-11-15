import { ApiProperty } from '@nestjs/swagger';
import { ArrayMaxSize, ArrayNotEmpty, ArrayUnique, Equals, IsArray, IsUUID } from 'class-validator';

export class DetachFileReferencesDto {
  @ApiProperty({ type: [String], description: '已确认解除引用的草稿版本 ID，最多 100 个' })
  @IsArray() @ArrayNotEmpty() @ArrayMaxSize(100) @ArrayUnique() @IsUUID('all', { each: true })
  versionIds!: string[];

  @ApiProperty({ enum: [true], description: '明确确认修改版本的文件成员' })
  @Equals(true)
  confirmed!: true;
}
