
import { Body, Controller, Delete, Get, HttpException, Param, ParseUUIDPipe, Post, Res } from '@nestjs/common';
import { ApiTags } from '@nestjs/swagger';
import { IsInt, IsNumber, IsNotEmpty, IsOptional, IsString, IsUUID, Matches, Max, MaxLength, Min, ValidateIf } from 'class-validator';
import { Response } from 'express';
import { CurrentUser } from '../auth/auth.decorators';
import { AuthUser } from '../auth/auth.types';
import { AIProvider } from './ai-provider';
import { AIChatService } from './ai-chat.service';

export class ChatDto { @IsString() @IsNotEmpty() @Matches(/\S/) @MaxLength(4000) message!: string; @IsOptional() @IsUUID() conversation_id?: string;
  @ValidateIf((_object, value) => value !== undefined) @IsNumber({}, { message: '回答随机性必须为数字' }) @Min(0) @Max(1.9) temperature?: number;
  @ValidateIf((_object, value) => value !== undefined) @IsNumber({}, { message: '候选采样范围必须为数字' }) @Min(0.01) @Max(1) top_p?: number;
  @ValidateIf((_object, value) => value !== undefined) @IsInt({ message: '最大输出长度必须为整数' }) @Min(1) @Max(8192) max_tokens?: number;
}
@ApiTags('ai')
@Controller('ai')
export class AIController {
  constructor(private readonly provider: AIProvider, private readonly chatService: AIChatService) {}
  @Get('status') async status() { return { ...await this.provider.healthCheck(), generation: this.provider.generationSettings }; }
  @Get('conversations') list(@CurrentUser() user: AuthUser) { return this.chatService.list(user); }
  @Get('conversations/:id') history(@CurrentUser() user: AuthUser, @Param('id', ParseUUIDPipe) id: string) { return this.chatService.history(user, id); }
  @Delete('conversations/:id/messages') clear(@CurrentUser() user: AuthUser, @Param('id', ParseUUIDPipe) id: string) { return this.chatService.clear(user, id); }
  @Post('chat') chat(@CurrentUser() user: AuthUser, @Body() body: ChatDto) { return this.chatService.chat(user, body.message.trim(), body.conversation_id, undefined, undefined, body); }
  @Post('chat/stream') async stream(@CurrentUser() user: AuthUser, @Body() body: ChatDto, @Res() response: Response) {
    const abort = new AbortController(); const close = () => abort.abort(); response.on('close', close);
    response.setHeader('Content-Type', 'text/event-stream; charset=utf-8'); response.setHeader('Cache-Control', 'no-cache, no-transform'); response.setHeader('X-Accel-Buffering', 'no'); response.flushHeaders();
    const heartbeat = setInterval(() => { if (!response.destroyed) response.write(': heartbeat\n\n'); }, 15000);
    try { await this.chatService.chat(user, body.message.trim(), body.conversation_id, event => { if (!response.destroyed) response.write(`data: ${JSON.stringify(event)}\n\n`); }, abort.signal, body); }
    catch (error) { if (!response.destroyed) response.write(`data: ${JSON.stringify({ type: 'error', data: { message: error instanceof HttpException ? error.message : 'AI 服务暂时不可用', status: error instanceof HttpException ? error.getStatus() : 500 } })}\n\n`); }
    finally { clearInterval(heartbeat); response.off('close', close); response.end(); }
  }
}

