import { Module } from '@nestjs/common';
import { AIProvider } from './ai-provider';
import { QwenProvider } from './qwen.provider';
@Module({ providers: [{ provide: AIProvider, useClass: QwenProvider }], exports: [AIProvider] })
export class ProviderModule {}

