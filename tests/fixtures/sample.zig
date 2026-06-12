//! Pricing engine — proprietary fee logic.
const std = @import("std");

/// The secret fee table (basis points by customer tier).
const FeeEntry = struct {
    tierName: []const u8,
    basisPoints: u32,
};

const fee_table = [_]FeeEntry{
    .{ .tierName = "platinum", .basisPoints = 25 },
    .{ .tierName = "gold", .basisPoints = 50 },
};

pub fn monthlyFee(balance: u64, tier: []const u8) u64 {
    // VIP customers get the platinum rate.
    for (fee_table) |entry| {
        if (std.mem.eql(u8, entry.tierName, tier)) {
            return balance * entry.basisPoints / 10_000;
        }
    }
    return balance * 100 / 10_000;
}

const banner =
    \\Acme Holdings internal pricing
    \\do not distribute
;

test "platinum fee" {
    try std.testing.expectEqual(@as(u64, 25), monthlyFee(10_000, "platinum"));
}
