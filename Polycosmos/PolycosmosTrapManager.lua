ModUtil.Mod.Register( "PolycosmosTrapManager" )

local TrapDataArray=
{
    "MoneyPunishment", --Takes some money away
    "HealthPunishment", --Takes some health away
}

local MoneyPunishmentRequest = 0
local HealthPunishmentRequest = 0

-------------------- Auxiliary function for checking if a item is a filler item
function PolycosmosTrapManager.IsTrapItem(string)
    return PolycosmosUtils.HasValue(TrapDataArray, string)
end

--------------------

function PolycosmosTrapManager.GiveTrapItem(item)
    if (item == "MoneyPunishment") then
        MoneyPunishmentRequest = MoneyPunishmentRequest + 1
    end
    if (item == "HealthPunishment") then
        HealthPunishmentRequest = HealthPunishmentRequest + 1
    end
    if (item == "DeathPunishment") then
        PolycosmosTrapManager.CreateLedger()
        
        --Activate if we can. If not, queue it as a trap
        if PolycosmosTrapManager.ShouldAvoidTriggerTraps() then
            GameState.TrapLedger["DeathPunishment"] = 1
        else
            PolycosmosEvents.SetDeathlinkFlag(true)
            GameState.TrapLedger["DeathPunishment"] = 0
            PolycosmosTrapManager.ProcessDeathTrap()
            PolycosmosEvents.SetDeathlinkFlag(false)
        end
        --Only allowing one deathtrap to avoid traps accumulating in short time. Also, annoying if they are too many.

    end
end

function PolycosmosTrapManager.FlushTrapItems()
    MoneyPunishmentRequest = 0
    HealthPunishmentRequest = 0
end

function PolycosmosTrapManager.ProcessDeathTrap()
    PolycosmosMessages.PrintToPlayer("Deathlink received!")
    if HasLastStand(CurrentRun.Hero) then
        CurrentRun.Hero.Health = 0
        CheckLastStand(CurrentRun.Hero, { })
        return false
    else
        Kill(CurrentRun.Hero, { }, { })
        return true
    end
end

function  PolycosmosTrapManager.CreateLedger()
    if (GameState.TrapLedger == nil) then
        GameState.TrapLedger = {}
        GameState.TrapLedger["MoneyPunishment"] = 0
        GameState.TrapLedger["HealthPunishment"] = 0
        GameState.TrapLedger["DeathPunishment"] = 0
    end
    --This is to mantain compatibility from 0.13.0 to newer version. Can erase in 0.14.0.
    if (GameState.TrapLedger["DeathPunishment"] == nil) then
        GameState.TrapLedger["DeathPunishment"] = 0
    end

end

--------------------

--Also procesing traps when room has finished setup. This seems to avoid corrupted save states.
--I would LOVE to have more control over this, but I literally cannot get it to work without annoying
-- issues :/
ModUtil.Path.Wrap("StartRoom", function( baseFunc, currentRun, currentRoom )
	local res = baseFunc(currentRun, currentRoom)
    PolycosmosTrapManager.ProcessTrapItems()
	return res
end)

--------------------

function PolycosmosTrapManager.ShouldAvoidTriggerTraps()
    local isItEarly = (CurrentRun == nil) or (CurrentRun.RunDepthCache == nil) or (CurrentRun.RunDepthCache < 1)
    local isInTransition = CurrentRun ~= nil and CurrentRun.CurrentRoom ~= nil and CurrentRun.CurrentRoom.ExitsUnlocked ~= nil
    local isInLoad = CurrentRun ~= nil and not IsEmpty( CurrentRun.BlockTimerFlags )
    return isItEarly or isInTransition or isInLoad or (not IsInputAllowed({}))
end

function PolycosmosTrapManager.ProcessTrapItems()
    PolycosmosTrapManager.CreateLedger()

    --I swear to god idk how we can get to this state which a null run but enemies, but this
    --game's architecture never cease to surprise me lol. Anyway, puttin ga couple of early exists for safety

    if PolycosmosTrapManager.ShouldAvoidTriggerTraps() then
        return
    end

    if (GameState.TrapLedger["DeathPunishment"] > 0) then
        PolycosmosEvents.SetDeathlinkFlag(true)
        GameState.TrapLedger["DeathPunishment"] = 0
        killedPlayer = PolycosmosTrapManager.ProcessDeathTrap()
        PolycosmosEvents.SetDeathlinkFlag(false)
        if (killedPlayer) then
            return
        end
    end

    if (MoneyPunishmentRequest > GameState.TrapLedger["MoneyPunishment"]) then
        local difLedger = MoneyPunishmentRequest - GameState.TrapLedger["MoneyPunishment"]
        local maxNumberPunishment = math.floor(CurrentRun.Money/100)
        local numberOfPunishments = math.min(maxNumberPunishment, difLedger)

        if (numberOfPunishments > 0) then
            CurrentRun.Money = math.max(CurrentRun.Money - 100*numberOfPunishments, 1)
		    ShowResourceUIs({ CombatOnly = false, UpdateIfShowing = true })
		    UpdateMoneyUI( CurrentRun.Money )

            PolycosmosMessages.PrintToPlayer("You got a Money punishment")

            GameState.TrapLedger["MoneyPunishment"] = GameState.TrapLedger["MoneyPunishment"] + numberOfPunishments
        end
    end

    if (HealthPunishmentRequest > GameState.TrapLedger["HealthPunishment"]) then
        local difLedger = HealthPunishmentRequest - GameState.TrapLedger["HealthPunishment"]
        local damage = CurrentRun.Hero.MaxHealth/4
        local maxNumberPunishment = math.floor(CurrentRun.Hero.MaxHealth/damage)
        local numberOfPunishments = math.min(maxNumberPunishment, difLedger)

        CurrentRun.Hero.Health  = math.max(CurrentRun.Hero.Health  - damage*numberOfPunishments,1)

        PolycosmosMessages.PrintToPlayer("You got a Health punishment")

        GameState.TrapLedger["HealthPunishment"] = GameState.TrapLedger["HealthPunishment"] + numberOfPunishments
    end

    MoneyPunishmentRequest = 0
    HealthPunishmentRequest = 0
end

--------------------
