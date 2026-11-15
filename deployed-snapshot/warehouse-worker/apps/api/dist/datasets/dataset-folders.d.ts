import { Prisma } from '@prisma/client';
import { ObjectStorage } from '../storage/object-storage';
export declare function ensureDatasetFolders(tx: Prisma.TransactionClient, storage: ObjectStorage, dataset: {
    id: string;
    name: string;
}, path: string): Promise<void>;
