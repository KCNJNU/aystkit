import { Prisma } from '@prisma/client';

/** A dataset may deliberately contain multiple robots. Keep project ACLs in AND. */
export function datasetRobotScope(robotId?: string): Prisma.DatasetWhereInput {
  return robotId ? { OR: [
    { metadata: { path: ['robotId'], equals: robotId } },
    { files: { some: { robotId } } },
    { versions: { some: { files: { some: { robotFile: { robotId } } } } } },
  ] } : {};
}
