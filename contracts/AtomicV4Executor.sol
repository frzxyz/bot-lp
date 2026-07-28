// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IV4ERC20 { function balanceOf(address) external view returns (uint256); function transfer(address,uint256) external returns(bool); function transferFrom(address,address,uint256) external returns(bool); function approve(address,uint256) external returns(bool); }
interface IV4Permit2 { function approve(address,address,uint160,uint48) external; }
interface IV4PositionManager {
    struct PoolKey { address currency0; address currency1; uint24 fee; int24 tickSpacing; address hooks; }
    function nextTokenId() external view returns(uint256);
    function ownerOf(uint256) external view returns(address);
    function getPositionLiquidity(uint256) external view returns(uint128);
    function getPoolAndPositionInfo(uint256) external view returns(PoolKey memory,uint256);
}

/// @notice Owner-only atomic USDG/Uniswap-V4 lifecycle wrapper. Swap calldata must be
/// built with this contract as both sender and recipient. It starts paused and never
/// permits an arbitrary call target or selector.
contract AtomicV4Executor {
    error Unauthorized(); error Paused(); error Reentered(); error WrongChain(); error Expired();
    error DeadlineTooFar(); error Invalid(); error PoolMismatch(); error DirtyBalance();
    error TransferFailed(); error Slippage(); error TargetDenied(); error SelectorDenied();
    uint256 public constant CHAIN_ID=4663;
    uint256 public constant MAX_DEADLINE_WINDOW=30 minutes;
    bytes4 public constant MODIFY_LIQUIDITIES=bytes4(keccak256("modifyLiquidities(bytes,uint256)"));
    address public immutable USDG; address public immutable POSITION_MANAGER;
    address public immutable PERMIT2; address public immutable SWAP_TARGET;
    address public owner; address public pendingOwner; bool public paused=true; uint256 private lock=1;
    mapping(bytes4=>bool) public swapSelectorAllowed;
    event Opened(uint256 indexed tokenId,address indexed token,bytes32 indexed poolId,uint256 usdgIn,uint256 tokenOut);
    event InventoryOpened(uint256 indexed tokenId,address indexed token,bytes32 indexed poolId,uint256 usdgCap,uint256 tokenCap);
    event Closed(uint256 indexed tokenId,address indexed token,uint256 settlementAmount,uint256 tokenAmount);
    event PauseSet(bool); event SwapSelectorSet(bytes4 indexed selector,bool allowed);
    event OwnershipTransferStarted(address indexed pending); event OwnershipTransferred(address indexed oldOwner,address indexed newOwner);
    event Rescued(address indexed token,uint256 amount);
    modifier onlyOwner(){if(msg.sender!=owner)revert Unauthorized();_;}
    modifier live(){if(block.chainid!=CHAIN_ID)revert WrongChain();if(paused)revert Paused();if(lock!=1)revert Reentered();lock=2;_;lock=1;}
    constructor(address owner_,address usdg,address posm,address permit2,address swapTarget){
        if(owner_==address(0)||usdg==address(0)||posm==address(0)||permit2==address(0)||swapTarget==address(0))revert Invalid();
        owner=owner_;USDG=usdg;POSITION_MANAGER=posm;PERMIT2=permit2;SWAP_TARGET=swapTarget;
    }
    function setPaused(bool x) external onlyOwner {paused=x;emit PauseSet(x);}
    function setSwapSelector(bytes4 s,bool x) external onlyOwner {if(!paused||s==bytes4(0))revert Invalid();swapSelectorAllowed[s]=x;emit SwapSelectorSet(s,x);}
    function transferOwnership(address x) external onlyOwner {if(x==address(0))revert Invalid();pendingOwner=x;emit OwnershipTransferStarted(x);}
    function acceptOwnership() external {if(msg.sender!=pendingOwner)revert Unauthorized();emit OwnershipTransferred(owner,msg.sender);owner=msg.sender;pendingOwner=address(0);}
    struct PoolIdentity {address currency0;address currency1;uint24 fee;int24 tickSpacing;address hooks;bytes32 poolId;}
    struct Open {address token;PoolIdentity pool;uint256 usdgAmount;uint256 swapAmount;uint256 minTokenOut;uint256 amount0Min;uint256 amount1Min;uint256 deadline;address swapTarget;bytes swapData;bytes mintData;}
    function atomicOpen(Open calldata p) external onlyOwner live returns(uint256 tokenId){
        _deadline(p.deadline);_poolInput(p.token,p.pool);
        if(p.usdgAmount==0||p.swapAmount==0||p.swapAmount>=p.usdgAmount||p.minTokenOut==0||p.amount0Min==0||p.amount1Min==0)revert Invalid();
        _target(p.swapTarget,p.swapData);_modify(p.mintData);_clean(p.token);_pull(USDG,msg.sender,p.usdgAmount);
        _approve(USDG,SWAP_TARGET,p.swapAmount);uint256 tb=_bal(p.token);_raw(SWAP_TARGET,p.swapData);_approve(USDG,SWAP_TARGET,0);
        uint256 tokenOut=_bal(p.token)-tb;if(tokenOut<p.minTokenOut)revert Slippage();
        _permit(USDG);_permit(p.token);uint256 next=IV4PositionManager(POSITION_MANAGER).nextTokenId();_raw(POSITION_MANAGER,p.mintData);
        tokenId=next; if(IV4PositionManager(POSITION_MANAGER).ownerOf(tokenId)!=owner)revert PoolMismatch();_checkPosition(tokenId,p.pool);
        _revoke(USDG);_revoke(p.token);_sweep(USDG);_sweep(p.token);emit Opened(tokenId,p.token,p.pool.poolId,p.usdgAmount,tokenOut);
    }
    /// @notice No-swap open funded by exact owner inventory caps; unused dust is refunded.
    struct InventoryOpen {address token;PoolIdentity pool;uint256 usdgAmount;uint256 tokenAmount;uint256 amount0Min;uint256 amount1Min;uint256 deadline;bytes mintData;}
    function atomicInventoryOpen(InventoryOpen calldata p) external onlyOwner live returns(uint256 tokenId){
        _deadline(p.deadline);_poolInput(p.token,p.pool);_modify(p.mintData);
        if(p.usdgAmount==0||p.tokenAmount==0||p.amount0Min==0||p.amount1Min==0)revert Invalid();
        _clean(p.token);_pull(USDG,msg.sender,p.usdgAmount);_pull(p.token,msg.sender,p.tokenAmount);
        _permit(USDG);_permit(p.token);uint256 next=IV4PositionManager(POSITION_MANAGER).nextTokenId();_raw(POSITION_MANAGER,p.mintData);
        tokenId=next;if(IV4PositionManager(POSITION_MANAGER).ownerOf(tokenId)!=owner)revert PoolMismatch();_checkPosition(tokenId,p.pool);
        _revoke(USDG);_revoke(p.token);_sweep(USDG);_sweep(p.token);emit InventoryOpened(tokenId,p.token,p.pool.poolId,p.usdgAmount,p.tokenAmount);
    }
    struct Close {uint256 tokenId;address token;PoolIdentity pool;uint128 liquidity;uint256 amount0Min;uint256 amount1Min;uint256 minSwapOut;uint256 minTotalUsdg;uint256 deadline;address swapTarget;bytes closeData;bytes swapData;}
    function atomicClose(Close calldata p) external onlyOwner live returns(uint256 settlementAmount,uint256 tokenAmount){
        _deadline(p.deadline);_poolInput(p.token,p.pool);if(p.liquidity==0||p.amount0Min==0||p.amount1Min==0)revert Invalid();
        _modify(p.closeData);_clean(p.token);_checkPosition(p.tokenId,p.pool);
        if(IV4PositionManager(POSITION_MANAGER).ownerOf(p.tokenId)!=owner||p.liquidity>IV4PositionManager(POSITION_MANAGER).getPositionLiquidity(p.tokenId))revert PoolMismatch();
        _raw(POSITION_MANAGER,p.closeData);settlementAmount=_bal(USDG);tokenAmount=_bal(p.token);
        _sweep(USDG);_sweep(p.token);emit Closed(p.tokenId,p.token,settlementAmount,tokenAmount);
    }
    function rescue(address token) external onlyOwner {if(!paused)revert Invalid();uint256 n=_bal(token);_safe(token,owner,n);emit Rescued(token,n);}
    function _poolInput(address token,PoolIdentity calldata p) internal view {if(token==address(0)||token==USDG||p.currency0>=p.currency1||p.tickSpacing<=0||(p.currency0!=USDG&&p.currency1!=USDG)||(p.currency0!=token&&p.currency1!=token)||keccak256(abi.encode(p.currency0,p.currency1,p.fee,p.tickSpacing,p.hooks))!=p.poolId)revert PoolMismatch();}
    function _checkPosition(uint256 id,PoolIdentity calldata p) internal view {(IV4PositionManager.PoolKey memory k,)=IV4PositionManager(POSITION_MANAGER).getPoolAndPositionInfo(id);if(k.currency0!=p.currency0||k.currency1!=p.currency1||k.fee!=p.fee||k.tickSpacing!=p.tickSpacing||k.hooks!=p.hooks)revert PoolMismatch();}
    function _target(address t,bytes calldata d) internal view {if(t!=SWAP_TARGET)revert TargetDenied();if(d.length<4||!swapSelectorAllowed[bytes4(d[:4])])revert SelectorDenied();}
    function _modify(bytes calldata d) internal pure {if(d.length<4||bytes4(d[:4])!=MODIFY_LIQUIDITIES)revert SelectorDenied();}
    function _deadline(uint256 d) internal view {if(d==0||d<block.timestamp)revert Expired();if(d>block.timestamp+MAX_DEADLINE_WINDOW)revert DeadlineTooFar();}
    function _permit(address t) internal {_approve(t,PERMIT2,type(uint256).max);IV4Permit2(PERMIT2).approve(t,POSITION_MANAGER,type(uint160).max,uint48(block.timestamp+MAX_DEADLINE_WINDOW));}
    function _revoke(address t) internal {IV4Permit2(PERMIT2).approve(t,POSITION_MANAGER,0,0);_approve(t,PERMIT2,0);}
    function _clean(address t) internal view {if(_bal(USDG)!=0||_bal(t)!=0)revert DirtyBalance();} function _bal(address t) internal view returns(uint256){return IV4ERC20(t).balanceOf(address(this));}
    function _pull(address t,address f,uint256 n) internal {uint256 b=_bal(t);_call(t,abi.encodeCall(IV4ERC20.transferFrom,(f,address(this),n)));if(_bal(t)-b!=n)revert TransferFailed();}
    function _safe(address t,address to,uint256 n) internal {if(n!=0)_call(t,abi.encodeCall(IV4ERC20.transfer,(to,n)));}
    function _approve(address t,address s,uint256 n) internal {(bool ok,bytes memory r)=t.call(abi.encodeCall(IV4ERC20.approve,(s,0)));if(!ok||(r.length!=0&&!abi.decode(r,(bool))))revert TransferFailed();if(n!=0)_call(t,abi.encodeCall(IV4ERC20.approve,(s,n)));}
    function _call(address t,bytes memory d) internal {(bool ok,bytes memory r)=t.call(d);if(!ok||(r.length!=0&&!abi.decode(r,(bool))))revert TransferFailed();}
    function _raw(address t,bytes calldata d) internal {(bool ok,bytes memory r)=t.call(d);if(!ok)assembly{revert(add(r,32),mload(r))}}
    function _sweep(address t) internal {_safe(t,owner,_bal(t));if(_bal(t)!=0)revert DirtyBalance();}
}
