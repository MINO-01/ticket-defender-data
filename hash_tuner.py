import time
import logging
import argon2
from argon2 import PasswordHasher

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def evaluate_argon2_performance(
    memory_cost: int, 
    time_cost: int, 
    parallelism: int, 
    test_payload: str = "010-1234-5678"
) -> float:
    """
    주어질 파라미터 조합으로 Argon2id 해싱 알고리즘의 소요 시간을 측정합니다.
    보안 강도(무차별 대입 공격 방어)와 시스템 트랜잭션 지연 시간 간의 최적의 균형점(약 250ms)을 찾기 위해 사용됩니다.
    
    Args:
        memory_cost: 사용할 메모리 크기 (KiB 단위)
        time_cost: 반복 연산 횟수 (CPU 부하)
        parallelism: 병렬 처리 스레드 수
        test_payload: 성능 측정에 사용할 샘플 평문 데이터
        
    Returns:
        해싱 처리에 소요된 시간 (밀리초 단위)
    """
    logger.info(f"성능 측정 파라미터 - Memory: {memory_cost}KiB, Time: {time_cost}, Parallelism: {parallelism}")
    
    password_hasher = PasswordHasher(
        time_cost=time_cost, 
        memory_cost=memory_cost, 
        parallelism=parallelism,
        type=argon2.low_level.Type.ID
    )
    
    start_time = time.time()
    hashed_result = password_hasher.hash(test_payload)
    elapsed_time_ms = (time.time() - start_time) * 1000
    
    logger.info(f"생성된 해시 길이: {len(hashed_result)} bytes (보안상 원문 출력 생략)")
    logger.info(f"소요 시간: {elapsed_time_ms:.2f} 밀리초")
    
    # 250ms 목표치와의 차이 분석 및 피드백 로깅
    if elapsed_time_ms < 200:
        logger.warning("성능 평가: 보안성 취약. 소요 시간이 너무 짧아 레인보우 테이블 및 무차별 대입 공격에 노출될 수 있습니다. memory_cost 또는 time_cost를 상향 조정하십시오.")
    elif elapsed_time_ms > 250:
        logger.error("성능 평가 실패: 250ms를 초과했습니다. 메인 서버의 병목 현상을 유발할 수 있으므로 파라미터를 하향 조정하십시오.")
        raise ValueError("Timeout: 250ms Limit Exceeded")
    else:
        logger.info("성능 평가: 최적화 완료. 보안과 성능의 이상적인 균형점(200~250ms)을 확보했습니다.")
        
    return elapsed_time_ms

if __name__ == "__main__":
    logger.info("Argon2id 알고리즘 파라미터 튜닝 테스트를 시작합니다.")
    
    # 테스트 환경 설정값 (대규모 더미 데이터 생성 목적의 고속 설정)
    TEST_ENV_CONFIG = {
        "memory_cost": 1024,
        "time_cost": 1,
        "parallelism": 2
    }
    
    # 운영 환경 설정값 (실제 서비스 보안 기준이 적용된 설정)
    PROD_ENV_CONFIG = {
        "memory_cost": 65540,
        "time_cost": 5,
        "parallelism": 2
    }
    
    logger.info("--- [시나리오 1] 테스트 환경 파라미터 검증 ---")
    evaluate_argon2_performance(**TEST_ENV_CONFIG)
    
    logger.info("\n--- [시나리오 2] 운영 환경 파라미터 검증 ---")
    evaluate_argon2_performance(**PROD_ENV_CONFIG)