export type TraceFields = {
    traceId: string;
    requestId?: string;
    userId?: string;
    datasetId?: string;
    fileId?: string;
    jobId?: string;
};
export declare class TraceContextService {
    private readonly storage;
    run<T>(fields: TraceFields, callback: () => T): T;
    get(): TraceFields | undefined;
    update(fields: Partial<TraceFields>): void;
}
